"""Prepare Dockerfile recipes from immutable project snapshots without execution."""

import json
import re
import tomllib

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import InvalidName, canonicalize_name
from packaging.version import InvalidVersion, Version

from backend.services.projects import list_project_files, read_project_file


def validate_uv_project(metadata, lock, python_pin=""):
    """Validate static inputs; uv performs authoritative lock checks at build time."""
    project = metadata.get("project")
    if not isinstance(project, dict):
        raise ValueError(
            "Dockerfile generation requires a [project] table in pyproject.toml."
        )
    if any(
        not isinstance(project.get(key, ""), str)
        for key in ("name", "version", "requires-python")
    ):
        raise ValueError(
            "project.name, project.version and requires-python must be strings."
        )
    try:
        name = canonicalize_name(project.get("name", ""), validate=True)
        version = Version(project.get("version", ""))
        python_spec = SpecifierSet(project.get("requires-python", ">=3.12"))
    except (InvalidName, InvalidVersion, InvalidSpecifier, TypeError) as exc:
        raise ValueError(
            "pyproject.toml needs a valid project.name, static project.version, "
            "and requires-python constraint. Use uv init and uv lock to prepare it."
        ) from exc
    if project.get("dynamic"):
        raise ValueError(
            "Dynamic project metadata requires a user-supplied Dockerfile."
        )
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or any(
        not isinstance(item, str) for item in dependencies
    ):
        raise ValueError("project.dependencies must be a list of requirement strings.")
    try:
        for dependency in dependencies:
            Requirement(dependency)
    except InvalidRequirement as exc:
        raise ValueError(
            "project.dependencies contains an invalid requirement."
        ) from exc
    tool = metadata.get("tool", {})
    uv = tool.get("uv", {}) if isinstance(tool, dict) else None
    if not isinstance(uv, dict) or uv.get("managed") is False or "workspace" in uv:
        raise ValueError(
            "Automatic generation supports managed, single-project uv environments. "
            "Supply a Dockerfile for unmanaged projects or workspaces."
        )
    packages = lock.get("package")
    if (
        type(lock.get("version")) is not int
        or lock["version"] != 1
        or not isinstance(packages, list)
    ):
        raise ValueError(
            "uv.lock is not a supported uv lockfile. Regenerate it with uv lock."
        )
    root = next(
        (
            item
            for item in packages
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and canonicalize_name(item["name"]) == name
            and item.get("source") in ({"virtual": "."}, {"editable": "."})
        ),
        None,
    )
    try:
        root_version = Version(root.get("version", "")) if root is not None else None
    except (InvalidVersion, TypeError):
        root_version = None
    if root_version != version:
        raise ValueError(
            "uv.lock does not match this project name/version. Run uv lock again."
        )

    if python_pin:
        if not re.fullmatch(r"3\.(?:10|11|12|13|14)(?:\.[0-9]+)?", python_pin):
            raise ValueError(
                ".python-version must pin CPython 3.10–3.14, for example 3.12 or 3.12.9."
            )
        candidates = [python_pin]
    else:
        candidates = ["3.12", "3.13", "3.14", "3.11", "3.10"]
    for candidate in candidates:
        if Version(candidate) in python_spec:
            return candidate
    raise ValueError(
        "No supported Python version satisfies requires-python. "
        "Pin a compatible version in .python-version or supply a Dockerfile."
    )


def prepare_dockerfile(project_id, entrypoint, arguments):
    entries = {
        entry["name"]: entry["type"]
        for entry in list_project_files(project_id)["entries"]
    }

    def read(name):
        if entries.get(name) != "file":
            raise ValueError(f"{name} must be a regular file in the project root.")
        return read_project_file(project_id, name)["content"]

    if "Dockerfile" in entries:
        return read("Dockerfile"), "project"

    if "pyproject.toml" not in entries or "uv.lock" not in entries:
        raise ValueError(
            "Automatic Dockerfile generation requires a uv project with root "
            "pyproject.toml and uv.lock files. Use uv init if needed, uv add to "
            "declare dependencies, and uv lock, then import the updated project. "
            "Alternatively, supply your own Dockerfile."
        )

    documents = {}
    for name in ("pyproject.toml", "uv.lock"):
        try:
            documents[name] = tomllib.loads(read(name))
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{name} is not valid TOML.") from exc
    python_pin = read(".python-version").strip() if ".python-version" in entries else ""
    if "uv.toml" in entries:
        raise ValueError("Move uv.toml settings into [tool.uv] or supply a Dockerfile.")
    python_version = validate_uv_project(
        documents["pyproject.toml"], documents["uv.lock"], python_pin
    )

    command = json.dumps(["python", f"/app/{entrypoint}", *arguments])
    return (
        "# Generated by Training Server. Build with the project root as context.\n"
        "# Declare system packages or custom CUDA setup in your own Dockerfile.\n"
        f"FROM python:{python_version}-slim-bookworm\n"
        "COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/\n"
        "WORKDIR /app\n"
        "ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never\n"
        'ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 GPU_JOB_OUTPUT_DIR=/output\n'
        "COPY . /app\n"
        "RUN uv sync --locked --no-dev --no-editable --python /usr/local/bin/python\n"
        "RUN uv pip check --python /opt/venv/bin/python\n"
        f"CMD {command}\n",
        "generated",
    )
