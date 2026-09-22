"""Persist validated training requests for later execution."""

from pathlib import PurePosixPath

from django.db import transaction
from django.utils import timezone

from backend.models import Dataset, TrainingJob
from backend.services.datasets import dataset_path
from backend.services.dockerfiles import prepare_dockerfile
from backend.services.projects import list_project_files


@transaction.atomic
def submit_training(project_id, entrypoint, epochs, dataset_id=None, requested_gpu="0"):
    job = TrainingJob(
        project_id=project_id,
        entrypoint=entrypoint,
        arguments=["--epochs", str(epochs)],
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
    job.full_clean()
    job.save()
    return job
