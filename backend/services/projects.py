"""Import and browse project snapshots and manage their datasets."""

import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from backend.models import Dataset, Project


def list_projects():
    """Return all imported projects, newest first, with stable ordering for ties."""
    return Project.objects.order_by("-created_at", "-id")


def list_ready_projects():
    """Return ready projects from either import source, newest first."""
    return list_projects().filter(status=Project.Status.READY)


class ProjectNotReady(ValueError):
    """The project cannot be browsed until its import is ready."""


def _tree_entries(project, tree):
    output = _git(project.storage_path, "ls-tree", "-z", tree)
    entries = []
    for record in output.split("\0"):
        if not record:
            continue
        metadata, name = record.split("\t", 1)
        mode, kind, object_id = metadata.split()
        entry_type = (
            "directory"
            if kind == "tree"
            else "submodule"
            if kind == "commit"
            else "symlink"
            if mode == "120000"
            else "file"
        )
        entries.append((name, entry_type, object_id))
    return entries


def list_project_files(project_id, path=""):
    """List one committed directory, without following symlinks or submodules.

    An empty path selects the root. Missing projects raise Project.DoesNotExist;
    invalid paths, unavailable snapshots and non-directory paths raise ValueError.
    Only relative paths are returned, never server storage locations.
    """
    project = Project.objects.get(pk=project_id)
    if project.status != Project.Status.READY:
        raise ProjectNotReady("Project is not ready.")
    if not project.storage_path or not project.resolved_commit:
        raise ValueError("Project snapshot is unavailable.")
    if (
        not isinstance(path, str)
        or PurePosixPath(path).is_absolute()
        or ".." in PurePosixPath(path).parts
        or "\\" in path
        or "\0" in path
    ):
        raise ValueError("Select a relative directory within the project.")
    parts = PurePosixPath(path).parts
    tree = f"{project.resolved_commit}^{{tree}}"
    for part in parts:
        match = next(
            (entry for entry in _tree_entries(project, tree) if entry[0] == part),
            None,
        )
        if match is None:
            raise ValueError("Directory does not exist in the project snapshot.")
        if match[1] != "directory":
            raise ValueError("Selected path is not a directory.")
        tree = match[2]
    entries = [
        {"name": name, "path": "/".join((*parts, name)), "type": kind}
        for name, kind, _ in _tree_entries(project, tree)
    ]
    entries.sort(key=lambda entry: (entry["type"] != "directory", entry["name"]))
    return {"path": "/".join(parts), "entries": entries}


def read_project_file(project_id, path):
    """Read a bounded UTF-8 file from the committed snapshot, never the worktree."""
    # Validate the full path using the same rules as directory browsing.
    if (
        not path
        or PurePosixPath(path).is_absolute()
        or ".." in PurePosixPath(path).parts
        or "\\" in path
        or "\0" in path
    ):
        raise ValueError("Select a relative file within the project.")
    relative = PurePosixPath(path)
    parent = str(relative.parent)
    listing = list_project_files(project_id, parent)
    entry = next(
        (item for item in listing["entries"] if item["name"] == relative.name), None
    )
    if entry is None or entry["type"] != "file":
        raise ValueError("Select a regular file in the project snapshot.")
    project = Project.objects.get(pk=project_id)
    tree = f"{project.resolved_commit}:{listing['path']}"
    object_id = next(
        item[2] for item in _tree_entries(project, tree) if item[0] == relative.name
    )
    if int(_git(project.storage_path, "cat-file", "-s", object_id)) > 1024 * 1024:
        raise ValueError("File is too large to preview (maximum 1 MiB).")
    raw = _git(project.storage_path, "cat-file", "blob", object_id, raw=True)
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Only UTF-8 text files can be previewed.") from exc
    if "\0" in content:
        raise ValueError("Binary files cannot be previewed.")
    return {"path": entry["path"], "content": content}


class ImportFailure(Exception):
    def __init__(self, project):
        self.project = project
        super().__init__(project.error)


# Executes git commands
def _git(directory, *arguments, raw=False):
    """Run a git command from spawned child process"""
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_ALLOW_PROTOCOL="https",
    )
    try:
        output = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "http.followRedirects=false",
                *arguments,
            ],
            cwd=directory,
            env=environment,
            check=True,
            capture_output=True,
            text=not raw,
            timeout=settings.PROJECT_GIT_TIMEOUT,
        ).stdout
        return output if raw else output.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        raise ValueError(
            "Git import failed; check the repository URL and Git availability."
        ) from exc


def _extract(upload, destination):
    with tempfile.TemporaryFile() as archive:
        size = 0
        for chunk in upload.chunks():
            size += len(chunk)
            if size > settings.PROJECT_UPLOAD_MAX_BYTES:
                raise ValueError("Uploaded ZIP exceeds the upload size limit.")
            archive.write(chunk)
        archive.seek(0)
        try:
            with zipfile.ZipFile(archive) as zipped:
                entries = zipped.infolist()
                if len(entries) > settings.PROJECT_UPLOAD_MAX_FILES:
                    raise ValueError("Uploaded ZIP contains too many files.")
                total = 0
                for entry in entries:
                    path = PurePosixPath(entry.filename)
                    if (
                        path.is_absolute()
                        or ".." in path.parts
                        or "\\" in entry.filename
                        or stat.S_ISLNK(entry.external_attr >> 16)
                    ):
                        raise ValueError(
                            "Uploaded ZIP contains an unsafe path or symbolic link."
                        )
                    # Uploaded Git metadata is not trusted; commit the actual files.
                    if ".git" in path.parts:
                        continue
                    total += entry.file_size
                    if total > settings.PROJECT_EXTRACT_MAX_BYTES:
                        raise ValueError(
                            "Extracted ZIP exceeds the storage size limit."
                        )
                    target = destination.joinpath(*path.parts)
                    if entry.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zipped.open(entry) as source, target.open("xb") as output:
                            shutil.copyfileobj(source, output)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            raise ValueError(
                "Upload must be a valid, unencrypted ZIP archive."
            ) from exc


def import_project(*, name, upload=None, repository_url=""):
    """Create a ready snapshot, or retain a failed import with its error."""
    ## Check for upload and validate url
    if (upload is None) == (not repository_url):
        raise ValueError("Provide exactly one ZIP file or repository URL.")
    if repository_url:
        url = urlsplit(repository_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError(
                "Repository URL must use HTTPS without embedded credentials."
            )
    project = Project.objects.create(
        name=name,
        source_type=Project.SourceType.UPLOAD
        if upload is not None
        else Project.SourceType.GIT,
        repository_url=repository_url,
    )
    destination = Path(settings.PROJECT_STORAGE_ROOT).resolve() / str(project.id)
    created = False
    try:
        destination.mkdir(parents=True, exist_ok=False)
        created = True
        if upload is not None:
            _extract(upload, destination)
            _git(destination, "init", "--template=")
            _git(destination, "add", "--all", "--force")
            _git(
                destination,
                "-c",
                "user.name=Training Server",
                "-c",
                "user.email=training@localhost",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--allow-empty",
                "-m",
                "Uploaded project snapshot",
            )
        else:
            _git(
                destination,
                "clone",
                "--template=",
                "--depth=1",
                "--",
                repository_url,
                ".",
            )
        project.resolved_commit = _git(destination, "rev-parse", "HEAD^{commit}")
        _git(destination, "checkout", "--detach", project.resolved_commit)
        project.storage_path = str(destination)
        project.status = Project.Status.READY
        project.full_clean()
        project.save()
        return project
    except (ValueError, OSError, ValidationError) as exc:
        if created:
            shutil.rmtree(destination)
        project.status = Project.Status.FAILED
        project.error = (
            str(exc)
            if isinstance(exc, ValueError)
            else "Unable to store imported project."
        )
        project.save(update_fields=["status", "error"])
        raise ImportFailure(project) from exc


# Project datasets


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
                pk=pk, training_job__status__in=["queued", "building", "running"]
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


# Immutable snapshot export


def export_snapshot(project, destination):
    """Materialize only regular committed files; never use the mutable worktree."""
    with tempfile.TemporaryDirectory(prefix="training-archive-") as temporary:
        archive = Path(temporary) / "snapshot.tar"
        _git(
            project.storage_path,
            "archive",
            "--format=tar",
            f"--output={archive}",
            project.resolved_commit,
        )
        total = 0
        with tarfile.open(archive) as source:
            for count, member in enumerate(source, start=1):
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or "\\" in member.name:
                    raise ValueError("Snapshot contains an unsafe build-context path.")
                if any(
                    part in {".git", ".venv", "venv", "__pycache__"}
                    for part in path.parts
                ):
                    continue
                if path.name == ".env" or path.name.startswith(".env."):
                    continue
                if count > settings.PROJECT_UPLOAD_MAX_FILES:
                    raise ValueError("Build context contains too many entries.")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError(
                        "Build context must not contain symlinks or special files."
                    )
                total += member.size
                if total > settings.PROJECT_EXTRACT_MAX_BYTES:
                    raise ValueError(
                        "Build context exceeds the project storage size limit."
                    )
                target = destination.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with (
                    source.extractfile(member) as incoming,
                    target.open("xb") as output,
                ):
                    shutil.copyfileobj(incoming, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    submodules = _git(project.storage_path, "ls-tree", "-r", project.resolved_commit)
    if any(line.startswith("160000 ") for line in submodules.splitlines()):
        raise ValueError(
            "Build context contains submodules; import their files explicitly."
        )
