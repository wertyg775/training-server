"""Bounded dataset uploads and repeatable retention cleanup."""

import shutil
import stat
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from backend.models import Dataset, Project
from backend.services.projects import ProjectNotReady


def dataset_path(dataset):
    return Path(settings.DATASET_STORAGE_ROOT).resolve() / str(dataset.pk)


def upload_dataset(project_id, upload):
    project = Project.objects.get(pk=project_id)
    if project.status != Project.Status.READY:
        raise ProjectNotReady("Project is not ready.")
    name = upload.name
    if (
        not name
        or len(name) > 255
        or name in {".", ".."}
        or any(c in name for c in "/\\\0")
    ):
        raise ValueError("Supply a valid dataset filename.")
    dataset = Dataset(
        project=project, name=name, expires_at=timezone.now() + timedelta(days=1)
    )
    destination = dataset_path(dataset)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        # Keep the temporary archive outside the payload directory.
        with tempfile.TemporaryFile() as incoming:
            for chunk in upload.chunks():
                dataset.size_bytes += len(chunk)
                if dataset.size_bytes > settings.DATASET_UPLOAD_MAX_BYTES:
                    raise ValueError("Dataset exceeds the upload size limit.")
                incoming.write(chunk)
            if not dataset.size_bytes:
                raise ValueError("Dataset is empty.")
            incoming.seek(0)
            if name.lower().endswith(".zip"):
                with zipfile.ZipFile(incoming) as archive:
                    if len(archive.infolist()) > settings.DATASET_MAX_FILES:
                        raise ValueError("Dataset contains too many files.")
                    total = 0
                    for entry in archive.infolist():
                        path = PurePosixPath(entry.filename)
                        kind = stat.S_IFMT(entry.external_attr >> 16)
                        if (
                            not path.parts
                            or path.is_absolute()
                            or ".." in path.parts
                            or "\\" in entry.filename
                            or "\0" in entry.filename
                            or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                        ):
                            raise ValueError(
                                "Dataset contains an unsafe path or special file."
                            )
                        target = destination.joinpath(*path.parts)
                        if entry.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                            continue
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(entry) as source, target.open("xb") as output:
                            while chunk := source.read(1024 * 1024):
                                total += len(chunk)
                                if total > settings.DATASET_EXTRACT_MAX_BYTES:
                                    raise ValueError(
                                        "Extracted dataset exceeds the storage size limit."
                                    )
                                output.write(chunk)
            else:
                with (destination / name).open("xb") as output:
                    shutil.copyfileobj(incoming, output)
        dataset.expires_at = timezone.now() + timedelta(days=1)
        dataset.full_clean()
        dataset.save()
        return dataset
    except Exception as exc:
        shutil.rmtree(destination)
        if isinstance(
            exc, (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError)
        ):
            raise ValueError(
                "Dataset could not be stored; use a regular file or valid unencrypted ZIP."
            ) from exc
        raise


def cleanup_datasets():
    """Lock against submission/retry; failed deletions remain eligible for retry."""
    deleted = 0
    for pk in Dataset.objects.filter(
        deleted_at__isnull=True, expires_at__lte=timezone.now()
    ).values_list("pk", flat=True):
        with transaction.atomic():
            dataset = Dataset.objects.select_for_update().get(pk=pk)
            if (
                dataset.deleted_at
                or not dataset.expires_at
                or dataset.expires_at > timezone.now()
            ):
                continue
            if Dataset.objects.filter(
                pk=pk, training_job__status__in=["queued", "running"]
            ).exists():
                continue
            destination = dataset_path(dataset)
            if destination.is_symlink():
                raise ValueError("Dataset directory must not be a symbolic link.")
            if destination.exists():
                shutil.rmtree(destination)
            dataset.deleted_at = timezone.now()
            dataset.save(update_fields=["deleted_at"])
            deleted += 1
    return deleted
