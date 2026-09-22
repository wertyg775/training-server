"""Start or retry a saved job using a built training image."""

from django.core.management.base import BaseCommand, CommandError

from backend.services.executions import start_job


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("job_id")
        parser.add_argument(
            "--image",
            required=True,
            help="Built image for this job, with its training command as CMD",
        )

    def handle(self, *args, **options):
        try:
            attempt = start_job(options["job_id"], options["image"])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"Execution {attempt.pk}: {attempt.state}. The training worker tracks completion and retention."
        )
