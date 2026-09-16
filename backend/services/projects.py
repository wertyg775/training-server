"""Import immutable project snapshots into configured local storage."""

import os
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError

from backend.models import Project


class ImportFailure(Exception):
    def __init__(self, project):
        self.project = project
        super().__init__(project.error)


def _git(directory, *arguments):
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
        return subprocess.run(
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
            text=True,
            timeout=settings.PROJECT_GIT_TIMEOUT,
        ).stdout.strip()
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
