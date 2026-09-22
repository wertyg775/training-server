"""Validate a saved training Dockerfile using a real Docker build and container."""

import json
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from backend.models import TrainingJob
from backend.services.environment_validation import validate_environment


class Command(BaseCommand):
    help = "Build a saved training recipe and check Python, script syntax, optional imports and CUDA."

    def add_arguments(self, parser):
        parser.add_argument("job_id", type=UUID)
        parser.add_argument(
            "--import-module", action="append", default=[], dest="imports"
        )
        parser.add_argument(
            "--check-cuda",
            action="store_true",
            help="Run a small PyTorch GPU operation on the job's requested GPU.",
        )
        parser.add_argument(
            "--run-entrypoint",
            action="store_true",
            help="Also run the image's default command, capped at 120 seconds. Temporary outputs are discarded.",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=900,
            help="Maximum build duration in seconds; runtime checks cap at 120 seconds.",
        )

    def handle(self, *args, **options):
        try:
            job = TrainingJob.objects.select_related("project").get(
                pk=options["job_id"]
            )
            report = validate_environment(
                job,
                imports=options["imports"],
                check_cuda=options["check_cuda"],
                run_entrypoint=options["run_entrypoint"],
                timeout=options["timeout"],
                progress=self.stdout.write,
            )
        except (TrainingJob.DoesNotExist, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report, indent=2))
        if report["status"] != "passed":
            raise CommandError(
                "Environment validation failed; see the saved report above."
            )
