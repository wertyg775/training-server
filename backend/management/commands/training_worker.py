"""Poll queued training jobs and manage builds, execution, and dataset expiry."""

import logging
import signal
import subprocess
import threading
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, close_old_connections

from backend.services.worker import TrainingWorker
from backend.services.worker_locks import file_lock

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument(
            "--gpus",
            nargs="+",
            default=settings.TRAINING_GPUS,
            help="GPU indexes or full UUIDs managed on this host (default: TRAINING_GPUS or 0)",
        )
        parser.add_argument("--poll-interval", type=float, default=20)
        parser.add_argument(
            "--once",
            action="store_true",
            help="Perform one recovery/queue sweep, then exit",
        )

    def handle(self, *args, **options):
        if options["poll_interval"] <= 0 or settings.TRAINING_BUILD_TIMEOUT <= 0:
            raise CommandError("Poll interval and build timeout must be positive.")
        with file_lock(
            Path(settings.TRAINING_WORK_ROOT).resolve() / "worker.lock"
        ) as acquired:
            if not acquired:
                raise CommandError(
                    "A training worker is already running for this work directory."
                )
            try:
                worker = TrainingWorker(options["gpus"])
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise CommandError(str(exc)) from exc
            stop = threading.Event()
            handlers = {}
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGINT, signal.SIGTERM):
                    handlers[signum] = signal.getsignal(signum)
                    signal.signal(signum, lambda *_: stop.set())
            self.stdout.write("Training worker started. Polling the database queue.")
            try:
                while not stop.is_set():
                    try:
                        worker.tick(stop=stop)
                    except DatabaseError:
                        close_old_connections()
                        logger.exception(
                            "Database unavailable; retrying after the poll interval"
                        )
                    if options["once"]:
                        break
                    stop.wait(options["poll_interval"])
            finally:
                for signum, handler in handlers.items():
                    signal.signal(signum, handler)
                close_old_connections()
            self.stdout.write(
                "Worker stopped; saved attempts will be recovered on its next start."
            )
