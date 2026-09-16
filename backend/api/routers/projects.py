from datetime import datetime
from uuid import UUID

from ninja import File, Form, Router, Schema
from ninja.files import UploadedFile
from pydantic import Field

from backend.services.projects import ImportFailure, import_project, list_projects

router = Router(tags=["projects"])


class ProjectResponse(Schema):
    id: UUID
    name: str
    source_type: str
    status: str
    resolved_commit: str
    error: str
    created_at: datetime


class ErrorResponse(Schema):
    detail: str


class GitImport(Schema):
    name: str = Field(min_length=1, max_length=255)
    repository_url: str = Field(min_length=1, max_length=2048)


@router.get("", response=list[ProjectResponse])
def get_projects(request):
    """List all projects from ZIP uploads and Git imports, newest first."""
    return list_projects()


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
