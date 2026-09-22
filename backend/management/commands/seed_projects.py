"""Import bundled example projects into the local development database."""

from io import BytesIO
from zipfile import ZipFile

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError

from backend.models import Project
from backend.services.projects import ImportFailure, import_project


class Command(BaseCommand):
    help = "Seed bundled example projects as browsable local Git snapshots."
    requires_migrations_checks = True

    def handle(self, *args, **options):
        examples = settings.BASE_DIR / "examples"
        if not examples.is_dir():
            raise CommandError(f"Example projects directory not found: {examples}")

        created = 0
        for directory in sorted(examples.iterdir()):
            if not directory.is_dir():
                continue
            name = f"Example: {directory.name}"
            if Project.objects.filter(
                name=name,
                source_type=Project.SourceType.UPLOAD,
                status=Project.Status.READY,
            ).exists():
                self.stdout.write(f"Skipped {name} (already seeded).")
                continue

            archive = BytesIO()
            with ZipFile(archive, "w") as zipped:
                for path in sorted(directory.rglob("*")):
                    relative = path.relative_to(directory)
                    if ".git" in relative.parts or "__pycache__" in relative.parts:
                        continue
                    if path.is_file() and not path.is_symlink():
                        zipped.write(path, relative.as_posix())
            upload = SimpleUploadedFile(
                f"{directory.name}.zip",
                archive.getvalue(),
                content_type="application/zip",
            )
            try:
                project = import_project(name=name, upload=upload)
            except ImportFailure as exc:
                raise CommandError(f"Unable to seed {name}: {exc}") from exc
            created += 1
            self.stdout.write(f"Created {name} ({project.pk}).")

        self.stdout.write(self.style.SUCCESS(f"Seeded {created} project(s)."))
