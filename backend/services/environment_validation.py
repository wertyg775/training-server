"""Build and smoke-test a saved recipe against its immutable source snapshot."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.utils import timezone

from backend.services.projects import _git


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
