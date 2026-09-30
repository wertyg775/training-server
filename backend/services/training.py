"""Prepare and validate training requests for later execution."""

import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
import tomllib
import uuid
from pathlib import Path, PurePosixPath

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import InvalidName, canonicalize_name
from packaging.version import InvalidVersion, Version

from backend.models import Dataset, TrainingJob
from backend.services.projects import (
    dataset_path,
    export_snapshot,
    list_project_files,
    read_project_file,
)


def list_training_jobs():
    """Return all training jobs, newest first, with project names for display."""
    return (
        TrainingJob.objects.select_related("dataset")
        .annotate(project_name=F("project__name"))
        .order_by("-created_at", "-id")
    )


@transaction.atomic
def submit_training(
    project_id,
    entrypoint,
    epochs=None,
    dataset_id=None,
    requested_gpu="0",
    dataset_target="",
    startup_check=False,
):
    job = TrainingJob(
        project_id=project_id,
        entrypoint=entrypoint,
        arguments=["--epochs", str(epochs)] if epochs is not None else [],
        dataset_target=dataset_target,
        startup_check=startup_check,
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
    if dataset_target:
        target = PurePosixPath(dataset_target)
        if (
            not dataset_id
            or target.is_absolute()
            or ".." in target.parts
            or not target.parts
            or any(c in dataset_target for c in "\\\0,\n\r")
            or target.parts[0] in {".git", ".venv"}
        ):
            raise ValueError(
                "Dataset location must be a relative path within the project."
            )
        if path == target or target in path.parents:
            raise ValueError("Dataset location must not hide the selected Python file.")
        job.dataset_target = target.as_posix()
    job.full_clean()
    job.save()
    return job


@transaction.atomic
def confirm_training(project_id, job_id):
    from backend.services.executions import hold_dataset

    job = TrainingJob.objects.select_for_update().get(pk=job_id, project_id=project_id)
    if not job.startup_check or job.status != "checked":
        raise ValueError("Wait for a successful startup check before submitting.")
    hold_dataset(job)
    job.startup_check = False
    job.status = "queued"
    job.finished_at = None
    job.save(update_fields=["startup_check", "status", "finished_at"])
    return job


# Dockerfile preparation


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


# Environment validation


def validate_environment(
    job,
    *,
    imports=(),
    check_cuda=False,
    run_entrypoint=False,
    timeout=900,
    runner=None,
    progress=None,
):
    """Persist measured results; run the image's default command only when requested."""
    if not job.dockerfile or not job.dockerfile_source:
        raise ValueError(
            "This job has no saved Dockerfile. Submit a new training request."
        )
    if timeout < 1:
        raise ValueError("Timeout must be positive.")
    for module in imports:
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module, flags=re.ASCII):
            raise ValueError("Import checks require dotted Python module names.")
    if check_cuda and not re.fullmatch(
        r"(?:[0-9]+|GPU-[a-fA-F0-9-]+)", job.requested_gpu
    ):
        raise ValueError("Select one GPU index or UUID.")
    runner = runner or subprocess.run
    report = {
        "status": "running",
        "started_at": timezone.now().isoformat(),
        "project_commit": job.project.resolved_commit,
        "dockerfile_sha256": hashlib.sha256(job.dockerfile.encode()).hexdigest(),
        "imports": list(imports),
        "cuda_requested": check_cuda,
        "entrypoint_requested": run_entrypoint,
        "steps": [],
    }
    job.environment_validation = report
    job.save(update_fields=["environment_validation"])
    image_id = None
    container = "training-validation-" + uuid.uuid4().hex
    image_tag = "training-validation:" + uuid.uuid4().hex

    def execute(step, arguments, limit):
        if progress:
            progress(f"{step}...")
        try:
            result = runner(
                arguments, capture_output=True, text=True, timeout=limit, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"{step} timed out after {limit} seconds.") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout or "No diagnostic output.")[-8000:]
            raise ValueError(f"{step} failed:\n{detail}")
        report["steps"].append(step)
        return result.stdout

    try:
        execute(
            "Docker access", ["docker", "info", "--format", "{{.ServerVersion}}"], 30
        )
        with tempfile.TemporaryDirectory(prefix="training-validation-") as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            export_snapshot(job.project, context)
            (context / "Dockerfile").write_text(job.dockerfile)
            iidfile = root / "image-id"
            execute(
                "Image build",
                [
                    "docker",
                    "build",
                    "--tag",
                    image_tag,
                    "--iidfile",
                    str(iidfile),
                    str(context),
                ],
                timeout,
            )
            image_id = iidfile.read_text().strip()
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
                image_id = None
                raise ValueError("Docker did not return a valid image ID.")
            report["image_id"] = image_id
            checks = (
                "import importlib, pathlib, sys; "
                f"p=pathlib.Path({json.dumps('/app/' + job.entrypoint)}); "
                "compile(p.read_bytes(), str(p), 'exec'); "
                f"[importlib.import_module(m) for m in {list(imports)!r}]; "
                "print('Python', sys.version); print('Script syntax and requested imports passed')"
            )
            if check_cuda:
                checks += (
                    "; import torch; assert torch.cuda.is_available(), 'CUDA unavailable'; "
                    "x=torch.ones(1, device='cuda'); assert x.sum().item()==1; "
                    "print('CUDA tensor operation passed')"
                )
            command = [
                "docker",
                "run",
                "--rm",
                "--name",
                container,
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,nosuid,size=256m",
                "--tmpfs",
                "/output:rw,nosuid,size=256m,mode=1777",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
            ]
            if check_cuda:
                command += ["--gpus", "device=" + job.requested_gpu]
            report["runtime_output"] = execute(
                "Runtime checks",
                [*command, "--entrypoint", "python", image_id, "-c", checks],
                min(timeout, 120),
            )[-8000:]
            if run_entrypoint:
                report["entrypoint_output"] = execute(
                    "Default command", [*command, image_id], min(timeout, 120)
                )[-8000:]
            report["status"] = "passed"
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
    finally:
        # A timed-out Docker client can leave its container running.
        cleanup = [["docker", "rm", "--force", container]]
        cleanup.append(["docker", "image", "rm", image_tag])
        for command in cleanup:
            try:
                runner(command, capture_output=True, text=True, timeout=30, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
        report["finished_at"] = timezone.now().isoformat()
        job.environment_validation = report
        job.save(update_fields=["environment_validation"])
    return report
