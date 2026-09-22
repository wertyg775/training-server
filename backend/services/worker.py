"""Single-host database queue worker, with one active attempt per GPU."""

import logging
import subprocess
import time

from django.db import DatabaseError, close_old_connections

from backend.models import ContainerExecution, TrainingJob
from backend.services.builds import poll_build
from backend.services.datasets import cleanup_datasets
from backend.services.executions import (
    ACTIVE,
    GPUUnavailable,
    fail_queued_job,
    reconcile_execution,
    reserve_job,
    resolve_gpu,
)
from training_server.executor import DockerExecutor

logger = logging.getLogger(__name__)


class TrainingWorker:
    def __init__(self, gpus, *, executor=None, cleanup_interval=60):
        self.executor = executor or DockerExecutor()
        self.devices = self.executor.gpu_devices()
        self.gpus = {resolve_gpu(gpu, self.devices) for gpu in gpus}
        if not self.gpus:
            raise ValueError("Configure at least one GPU for the worker.")
        self.cleanup_interval = cleanup_interval
        self.next_cleanup = 0
        self.children = []

    def tick(self, stop=None):
        close_old_connections()
        self.children = [child for child in self.children if child.poll() is None]
        # Reconcile existing work before claiming: an exited container releases its
        # reservation here, while a Docker outage keeps the reservation intact.
        for attempt in ContainerExecution.objects.filter(state__in=ACTIVE).iterator():
            if stop is not None and stop.is_set():
                return
            try:
                if attempt.state == "building":
                    child = poll_build(attempt.pk)
                    if child is not None:
                        self.children.append(child)
                else:
                    reconcile_execution(attempt.pk, self.executor)
            except (OSError, ValueError, subprocess.SubprocessError, DatabaseError):
                logger.exception(
                    "Could not advance execution %s; retaining its reservation",
                    attempt.pk,
                )
        for job in (
            TrainingJob.objects.filter(status="queued")
            .order_by("created_at", "id")
            .iterator()
        ):
            if stop is not None and stop.is_set():
                break
            try:
                gpu = resolve_gpu(job.requested_gpu, self.devices)
                if gpu not in self.gpus:
                    continue
                attempt = reserve_job(job.pk, self.devices, queued_only=True)
                if attempt:
                    child = poll_build(attempt.pk)
                    if child is not None:
                        self.children.append(child)
            except GPUUnavailable:
                continue
            except ValueError as exc:
                fail_queued_job(job.pk, exc)
            except (OSError, subprocess.SubprocessError, DatabaseError):
                logger.exception("Could not claim job %s; will try again", job.pk)
        if time.monotonic() >= self.next_cleanup:
            try:
                cleanup_datasets()
            except (OSError, ValueError, DatabaseError):
                logger.exception("Dataset cleanup failed; will retry on the next sweep")
            self.next_cleanup = time.monotonic() + self.cleanup_interval
