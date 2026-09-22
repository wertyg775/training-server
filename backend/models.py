"""Imported project snapshots, training requests and execution attempts."""

import re
import uuid
from pathlib import PurePosixPath

from django.core.exceptions import ValidationError
from django.db import models


class Project(models.Model):
    """One fixed Git snapshot; updated code requires a new import.

    Both remote imports and uploads become self-contained Git repositories.
    Import preparation populates storage_path and resolved_commit before marking
    the project ready. Ready project contents must not be modified in place.
    The importer must verify the repository and commit exist at storage_path.
    """

    class SourceType(models.TextChoices):
        GIT = "git", "Git repository"
        UPLOAD = "upload", "Uploaded folder"

    class Status(models.TextChoices):
        IMPORTING = "importing", "Importing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    source_type = models.CharField(max_length=16, choices=SourceType.choices)
    repository_url = models.URLField(max_length=2048, blank=True)
    requested_revision = models.CharField(max_length=255, blank=True)
    resolved_commit = models.CharField(
        max_length=64,
        blank=True,
        help_text="Exact Git commit for either import source.",
    )
    storage_path = models.TextField(
        blank=True,
        help_text="Server-managed path to the self-contained Git repository.",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.IMPORTING
    )
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def clean(self):
        super().clean()
        if self.source_type == self.SourceType.GIT and not self.repository_url:
            raise ValidationError(
                {"repository_url": "Git imports require a repository URL."}
            )
        if self.status == self.Status.READY:
            if not self.storage_path:
                raise ValidationError(
                    {"storage_path": "Ready projects require a stored Git repository."}
                )
            if not self.resolved_commit:
                raise ValidationError(
                    {"resolved_commit": "Ready projects require a Git commit."}
                )


class Dataset(models.Model):
    """One job's uploaded data; keep metadata after removing the payload."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="datasets"
    )
    name = models.CharField(max_length=255)
    size_bytes = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)


class TrainingJob(models.Model):
    """A request to run a Python entry point from an imported project snapshot."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        BUILDING = "building", "Building image"
        RUNNING = "running", "Running"
        FINISHED = "finished", "Finished"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    dataset = models.OneToOneField(
        Dataset,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="training_job",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.PROTECT,
        related_name="training_jobs",
    )
    entrypoint = models.CharField(max_length=1024)
    arguments = models.JSONField(default=list, blank=True)
    requested_gpu = models.CharField(max_length=128, default="0")
    dockerfile = models.TextField(blank=True)
    dockerfile_source = models.CharField(max_length=16, blank=True)
    environment_validation = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]

    def clean(self):
        super().clean()
        if not re.fullmatch(r"(?:[0-9]+|GPU-[a-fA-F0-9-]+)", self.requested_gpu):
            raise ValidationError({"requested_gpu": "Select a GPU index or full UUID."})
        path = PurePosixPath(self.entrypoint)
        if (
            not self.entrypoint
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in self.entrypoint
            or "\0" in self.entrypoint
            or path.suffix != ".py"
        ):
            raise ValidationError(
                {"entrypoint": "Select a relative Python file within the project."}
            )
        if not isinstance(self.arguments, list) or any(
            not isinstance(argument, str) or "\0" in argument
            for argument in self.arguments
        ):
            raise ValidationError(
                {"arguments": "Arguments must be a list of strings without null bytes."}
            )


class ContainerExecution(models.Model):
    """One attempt to run a training job in a Docker container."""

    class State(models.TextChoices):
        BUILDING = "building", "Building image"
        STARTING = "starting", "Starting"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    training_job = models.ForeignKey(
        TrainingJob,
        on_delete=models.PROTECT,
        related_name="executions",
    )
    container_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    image_digest = models.CharField(max_length=255, blank=True)
    build_token = models.UUIDField(null=True, blank=True)
    build_attempts = models.PositiveIntegerField(default=0)
    build_started_at = models.DateTimeField(null=True, blank=True)
    build_finished_at = models.DateTimeField(null=True, blank=True)
    build_log = models.TextField(blank=True)
    assigned_gpu = models.CharField(max_length=128, blank=True)
    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.STARTING,
    )
    output_path = models.TextField(blank=True)
    exit_code = models.IntegerField(null=True, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["training_job"],
                condition=models.Q(state__in=["building", "starting", "running"]),
                name="one_active_execution_per_job",
            ),
            models.UniqueConstraint(
                fields=["assigned_gpu"],
                condition=models.Q(state__in=["building", "starting", "running"])
                & ~models.Q(assigned_gpu=""),
                name="one_active_execution_per_gpu",
            ),
        ]
