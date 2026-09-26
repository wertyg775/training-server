"""Persist validated training requests for later execution."""

from pathlib import PurePosixPath

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from backend.models import Dataset, TrainingJob
from backend.services.datasets import dataset_path
from backend.services.dockerfiles import prepare_dockerfile
from backend.services.projects import list_project_files


def list_training_jobs():
    """Return all training jobs, newest first, with project names for display."""
    return (
        TrainingJob.objects.select_related("dataset")
        .annotate(project_name=F("project__name"))
        .order_by("-created_at", "-id")
    )


@transaction.atomic
def submit_training(
    project_id,
    entrypoint,
    epochs=None,
    dataset_id=None,
    requested_gpu="0",
    dataset_target="",
    startup_check=False,
):
    job = TrainingJob(
        project_id=project_id,
        entrypoint=entrypoint,
        arguments=["--epochs", str(epochs)] if epochs is not None else [],
        dataset_target=dataset_target,
        startup_check=startup_check,
        requested_gpu=requested_gpu,
    )
    # Validate paths before looking up the snapshot or creating any records.
    job.clean()
    path = PurePosixPath(entrypoint)
    directory = list_project_files(project_id, str(path.parent))
    if not any(
        entry["name"] == path.name and entry["type"] == "file"
        for entry in directory["entries"]
    ):
        raise ValueError("Select a Python file in the project snapshot.")
    job.entrypoint = path.as_posix()
    job.dockerfile, job.dockerfile_source = prepare_dockerfile(
        project_id, job.entrypoint, job.arguments
    )
    if dataset_id:
        dataset = (
            Dataset.objects.select_for_update()
            .filter(pk=dataset_id, project_id=project_id)
            .first()
        )
        if (
            dataset is None
            or dataset.deleted_at
            or (dataset.expires_at and dataset.expires_at <= timezone.now())
            or TrainingJob.objects.filter(dataset=dataset).exists()
            or not dataset_path(dataset).is_dir()
        ):
            raise ValueError(
                "Dataset is unavailable or already assigned to a job. Upload it again."
            )
        job.dataset = dataset
        dataset.expires_at = None
        dataset.save(update_fields=["expires_at"])
    if dataset_target:
        target = PurePosixPath(dataset_target)
        if (
            not dataset_id
            or target.is_absolute()
            or ".." in target.parts
            or not target.parts
            or any(c in dataset_target for c in "\\\0,\n\r")
            or target.parts[0] in {".git", ".venv"}
        ):
            raise ValueError(
                "Dataset location must be a relative path within the project."
            )
        if path == target or target in path.parents:
            raise ValueError("Dataset location must not hide the selected Python file.")
        job.dataset_target = target.as_posix()
    job.full_clean()
    job.save()
    return job


@transaction.atomic
def confirm_training(project_id, job_id):
    from backend.services.executions import hold_dataset

    job = TrainingJob.objects.select_for_update().get(pk=job_id, project_id=project_id)
    if not job.startup_check or job.status != "checked":
        raise ValueError("Wait for a successful startup check before submitting.")
    hold_dataset(job)
    job.startup_check = False
    job.status = "queued"
    job.finished_at = None
    job.save(update_fields=["startup_check", "status", "finished_at"])
    return job
