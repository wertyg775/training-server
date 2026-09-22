"""Persist validated training requests for later execution."""

from pathlib import PurePosixPath

from backend.models import TrainingJob
from backend.services.projects import list_project_files


def submit_training(project_id, entrypoint, epochs):
    job = TrainingJob(
        project_id=project_id,
        entrypoint=entrypoint,
        arguments=["--epochs", str(epochs)],
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
    job.full_clean()
    job.save()
    return job
