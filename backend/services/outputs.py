"""List and safely open files produced by training executions."""

import os
import stat
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.db.models import F, Window
from django.db.models.functions import RowNumber

from backend.models import ContainerExecution

MAX_LISTED_FILES = 1000
MAX_SCANNED_ENTRIES = 5000
MAX_FOLDER_DEPTH = 16


def _output_directory(execution):
    root = Path(settings.TRAINING_OUTPUT_ROOT).resolve()
    expected = root / str(execution.training_job_id) / str(execution.pk)
    if Path(execution.output_path) != expected:
        return None
    return expected


def _files_in(directory):
    files = []
    pending = [(directory, "", 0)]
    scanned = 0
    truncated = False
    while pending and not truncated:
        folder, prefix, depth = pending.pop()
        try:
            with os.scandir(folder) as iterator:
                for entry in iterator:
                    scanned += 1
                    if scanned > MAX_SCANNED_ENTRIES or len(files) >= MAX_LISTED_FILES:
                        truncated = True
                        break
                    if "\\" in entry.name:
                        continue
                    try:
                        entry.name.encode("utf-8")
                    except UnicodeEncodeError:
                        continue
                    relative = f"{prefix}{entry.name}"
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if depth < MAX_FOLDER_DEPTH:
                                pending.append(
                                    (Path(entry.path), f"{relative}/", depth + 1)
                                )
                            else:
                                truncated = True
                        elif entry.is_file(follow_symlinks=False):
                            files.append(
                                {
                                    "path": relative,
                                    "size_bytes": entry.stat(
                                        follow_symlinks=False
                                    ).st_size,
                                }
                            )
                    except OSError:
                        continue
        except OSError:
            continue
    return sorted(files, key=lambda file: file["path"].lower()), truncated


def list_output_runs():
    """Return runs with saved files, using human-readable job context."""
    executions = (
        ContainerExecution.objects.select_related("training_job__project")
        .annotate(
            run_number=Window(
                expression=RowNumber(),
                partition_by=[F("training_job_id")],
                order_by=[F("created_at").asc(), F("id").asc()],
            )
        )
        .order_by("-created_at", "-id")
    )
    runs = []
    for execution in executions:
        directory = _output_directory(execution)
        if directory is None or not directory.is_dir() or directory.is_symlink():
            continue
        files, truncated = _files_in(directory)
        if not files and not truncated:
            continue
        runs.append(
            {
                "execution_id": execution.pk,
                "project_name": execution.training_job.project.name,
                "entrypoint": execution.training_job.entrypoint,
                "run_number": execution.run_number,
                "state": execution.state,
                "created_at": execution.created_at,
                "finished_at": execution.finished_at,
                "files": files,
                "truncated": truncated,
            }
        )
    return runs


def open_output_file(execution, path):
    """Open one regular file without following symlinks in any path segment."""
    relative = PurePosixPath(path)
    if (
        not path
        or relative.is_absolute()
        or not relative.parts
        or any(part in {".", ".."} for part in relative.parts)
        or "\\" in path
        or "\0" in path
    ):
        raise ValueError("Select a relative output file.")
    if _output_directory(execution) is None:
        raise FileNotFoundError(path)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(Path(settings.TRAINING_OUTPUT_ROOT).resolve(), directory_flags)
    try:
        for part in (
            str(execution.training_job_id),
            str(execution.pk),
            *relative.parts[:-1],
        ):
            next_fd = os.open(part, directory_flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(
            relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            os.close(file_fd)
            raise FileNotFoundError(path)
        return os.fdopen(file_fd, "rb")
    finally:
        os.close(fd)
