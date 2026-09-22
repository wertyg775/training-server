from datetime import datetime
from typing import Literal
from uuid import UUID

from ninja import File, Form, Router, Schema
from ninja.files import UploadedFile
from pydantic import Field

from backend.models import Project
from backend.services.projects import (
    ImportFailure,
    ProjectNotReady,
    import_project,
    list_project_files,
    list_projects,
    list_ready_projects,
    read_project_file,
)

router = Router(tags=["projects"])


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
