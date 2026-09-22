from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.management.base import BaseCommand, CommandError

from backend.services.executions import queue_retry


class Command(BaseCommand):
    help = "Queue a completed job for another worker attempt while its dataset is available."

    def add_arguments(self, parser):
        parser.add_argument("job_id")

    def handle(self, *args, **options):
        try:
            queue_retry(options["job_id"])
        except (ObjectDoesNotExist, ValidationError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write("Job queued for retry.")
