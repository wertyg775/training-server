import uuid
from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models


def require_empty_jobs(apps, schema_editor):
    if (
        apps.get_model("backend", "TrainingJob")
        .objects.using(schema_editor.connection.alias)
        .exists()
    ):
        raise RuntimeError(
            "Existing image-based training jobs cannot be mapped to Git snapshots automatically. "
            "Migrate or archive those jobs before applying this migration."
        )


class Migration(migrations.Migration):
    dependencies: ClassVar = [("backend", "0001_initial")]

    operations: ClassVar = [
        migrations.RunPython(require_empty_jobs, migrations.RunPython.noop),
        migrations.CreateModel(
            name="Project",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        default=uuid.uuid4,
                        editable=False,
                        serialize=False,
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                (
                    "source_type",
                    models.CharField(
                        max_length=16,
                        choices=[
                            ("git", "Git repository"),
                            ("upload", "Uploaded folder"),
                        ],
                    ),
                ),
                ("repository_url", models.URLField(max_length=2048, blank=True)),
                ("requested_revision", models.CharField(max_length=255, blank=True)),
                (
                    "resolved_commit",
                    models.CharField(
                        max_length=64,
                        blank=True,
                        help_text="Exact Git commit for either import source.",
                    ),
                ),
                (
                    "storage_path",
                    models.TextField(
                        blank=True,
                        help_text="Server-managed path to the self-contained Git repository.",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        max_length=16,
                        default="importing",
                        choices=[
                            ("importing", "Importing"),
                            ("ready", "Ready"),
                            ("failed", "Failed"),
                        ],
                    ),
                ),
                ("error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.RenameField("trainingjob", "command", "arguments"),
        migrations.RemoveField("trainingjob", "image"),
        migrations.AddField(
            "trainingjob", "entrypoint", models.CharField(max_length=1024)
        ),
        migrations.AddField(
            "trainingjob",
            "project",
            models.ForeignKey(
                to="backend.project",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="training_jobs",
            ),
        ),
    ]
