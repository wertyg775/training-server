from datetime import datetime
from typing import Literal
from uuid import UUID

from django.core.exceptions import ValidationError
from django.http import HttpResponse
from ninja import File, Form, Router, Schema
from ninja.files import UploadedFile
from pydantic import Field

from backend.models import Project, TrainingJob
from backend.services.projects import (
    ImportFailure,
    ProjectNotReady,
    import_project,
    list_project_files,
    list_projects,
    list_ready_projects,
    read_project_file,
)
from backend.services.training import submit_training

router = Router(tags=["projects"])


class TrainingRequest(Schema):
    entrypoint: str = Field(min_length=1, max_length=1024)
    epochs: int = Field(strict=True, ge=1, le=2147483647)


class TrainingResponse(Schema):
    id: UUID
    project_id: UUID
    entrypoint: str
    arguments: list[str]
    requested_gpu: str
    dockerfile_source: str
    created_at: datetime


class ProjectResponse(Schema):
    id: UUID
    name: str
    source_type: str
    repository_url: str
    requested_revision: str
    status: str
    resolved_commit: str
    error: str
    created_at: datetime


class ErrorResponse(Schema):
    detail: str


class DirectoryEntry(Schema):
    name: str
    path: str
    type: Literal["directory", "file", "symlink", "submodule"]


class DirectoryResponse(Schema):
    path: str
    entries: list[DirectoryEntry]


class FileResponse(Schema):
    path: str
    content: str


class GitImport(Schema):
    name: str = Field(min_length=1, max_length=255)
    repository_url: str = Field(min_length=1, max_length=2048)


@router.get("", response=list[ProjectResponse])
def get_projects(request):
    """List all projects from ZIP uploads and Git imports, newest first."""
    return list_projects()


@router.get("/ready", response=list[ProjectResponse])
def get_ready_projects(request):
    """List ready projects from either import source, newest first."""
    return list_ready_projects()


@router.get(
    "/{project_id}/files",
    response={
        200: DirectoryResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        409: ErrorResponse,
    },
)
def get_project_files(request, project_id: UUID, path: str = ""):
    """List a ready project's committed directory; omit path for the root."""
    try:
        return list_project_files(project_id, path)
    except Project.DoesNotExist:
        return 404, {"detail": "Project not found."}
    except ProjectNotReady as exc:
        return 409, {"detail": str(exc)}
    except ValueError as exc:
        return 400, {"detail": str(exc)}


@router.get(
    "/{project_id}/file",
    response={
        200: FileResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        409: ErrorResponse,
    },
)
def get_project_file(request, project_id: UUID, path: str):
    try:
        return read_project_file(project_id, path)
    except Project.DoesNotExist:
        return 404, {"detail": "Project not found."}
    except ProjectNotReady as exc:
        return 409, {"detail": str(exc)}
    except ValueError as exc:
        return 400, {"detail": str(exc)}


@router.post(
    "/{project_id}/training-jobs",
    response={
        201: TrainingResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        409: ErrorResponse,
    },
)
def create_training_request(request, project_id: UUID, payload: TrainingRequest):
    """Save a training request; execution is handled separately."""
    try:
        return 201, submit_training(project_id, payload.entrypoint, payload.epochs)
    except Project.DoesNotExist:
        return 404, {"detail": "Project not found."}
    except ProjectNotReady as exc:
        return 409, {"detail": str(exc)}
    except ValidationError as exc:
        return 400, {"detail": " ".join(exc.messages)}
    except ValueError as exc:
        return 400, {"detail": str(exc)}


@router.get(
    "/{project_id}/training-jobs/{job_id}/dockerfile",
    response={200: str, 404: ErrorResponse},
)
def download_training_dockerfile(request, project_id: UUID, job_id: UUID):
    job = TrainingJob.objects.filter(pk=job_id, project_id=project_id).first()
    if job is None or not job.dockerfile_source:
        return 404, {"detail": "Training Dockerfile not found."}
    response = HttpResponse(job.dockerfile, content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="Dockerfile"'
    return response


def _import(**kwargs):
    try:
        return 201, import_project(**kwargs)
    except ImportFailure as exc:
        return 400, exc.project
    except ValueError as exc:
        return 422, {"detail": str(exc)}


@router.post(
    "/upload", response={201: ProjectResponse, 400: ProjectResponse, 422: ErrorResponse}
)
def upload_project(request, name: Form[str], file: File[UploadedFile]):
    """Import a folder as a ZIP archive, preserving its relative file paths."""
    if not name.strip() or len(name) > 255:
        return 422, {"detail": "Name must contain between 1 and 255 characters."}
    return _import(name=name, upload=file)


@router.post(
    "/git", response={201: ProjectResponse, 400: ProjectResponse, 422: ErrorResponse}
)
def import_git_project(request, payload: GitImport):
    """Import the default branch of an HTTPS Git repository."""
    if not payload.name.strip():
        return 422, {"detail": "Name must not be blank."}
    return _import(**payload.model_dump())
