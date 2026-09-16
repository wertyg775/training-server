"""Training requests and their individual container execution attempts."""

import uuid

from django.core.exceptions import ValidationError
from django.db import models


class TrainingJob(models.Model):
    """What the user requested, independent of any execution attempt."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    repository_url = models.URLField(max_length=2048, blank=True)
    revision = models.CharField(max_length=255, blank=True)
    project_archive = models.FileField(
        upload_to="training-projects/%Y/%m/%d/", blank=True
    )
    config_path = models.CharField(max_length=1024, blank=True)
    command = models.JSONField(default=list, blank=True)
    requested_gpu = models.CharField(max_length=128, default="0")
    created_at = models.DateTimeField(auto_now_add=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]

    def clean(self):
        super().clean()
        if bool(self.repository_url) == bool(self.project_archive):
            raise ValidationError(
                "Provide either a Git repository URL or a project archive, not both."
            )
        if self.revision and not self.repository_url:
            raise ValidationError({"revision": "A revision requires a Git repository."})


class ContainerExecution(models.Model):
    """One attempt to run a training job in a Docker container."""

    class State(models.TextChoices):
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
