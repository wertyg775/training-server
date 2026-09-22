"""Reconcile Docker completion and remove expired dataset files."""

import subprocess

from django.core.management.base import BaseCommand, CommandError

from backend.models import ContainerExecution
from backend.services.datasets import cleanup_datasets
from backend.services.executions import ACTIVE, reconcile_execution


class Command(BaseCommand):
    help = __doc__

    def handle(self, *args, **options):
        failures = []
        for pk in ContainerExecution.objects.filter(state__in=ACTIVE).values_list(
            "pk", flat=True
        ):
            try:
                reconcile_execution(pk)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                failures.append(f"{pk}: {exc}")
        deleted = cleanup_datasets()
        self.stdout.write(f"Deleted {deleted} expired dataset(s).")
        if failures:
            raise CommandError("\n".join(failures))
