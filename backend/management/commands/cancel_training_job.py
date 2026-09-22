from django.core.management.base import BaseCommand, CommandError

from backend.services.executions import cancel_job


class Command(BaseCommand):
    help = "Cancel a saved training job and start its dataset retention period."

    def add_arguments(self, parser):
        parser.add_argument("job_id")

    def handle(self, *args, **options):
        try:
            cancel_job(options["job_id"])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            "Cancellation requested; the worker will stop any active image build."
        )
