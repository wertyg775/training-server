"""Start and recover isolated builders without holding database transactions."""

import fcntl
import json
import re
import shutil
import subprocess
import sys
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from backend.build_image import write_json
from backend.models import ContainerExecution, TrainingJob
from backend.services.executions import finish_execution
from backend.services.worker_locks import attempt_directory, attempt_lock


def build_directory(attempt):
    return attempt_directory(attempt.pk) / str(attempt.build_token)


def poll_build(execution_id):
    """Return a new builder process when launched, otherwise None."""
    with attempt_lock(execution_id) as acquired:
        if not acquired:
            return None
        attempt = ContainerExecution.objects.select_related(
            "training_job__project"
        ).get(pk=execution_id)
        if attempt.state != "building":
            return None
        directory = build_directory(attempt) if attempt.build_token else None
        # A builder inherits this lock from its launching worker. It remains held
        # if that worker exits, preventing duplicate builders on restart.
        lock_path = attempt_directory(attempt.pk) / "build.lock"
        with lock_path.open("a+") as build_lock:
            try:
                fcntl.flock(build_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if attempt.training_job.cancel_requested_at and directory:
                    (directory / "cancel").touch()
                return None
            if directory and (directory / "result.json").exists():
                try:
                    result = json.loads((directory / "result.json").read_text())
                    completed = parse_datetime(result["finished_at"])
                    if completed is None or not isinstance(result["success"], bool):
                        raise ValueError("Invalid build completion metadata.")
                    if result["success"] and not re.fullmatch(
                        r"sha256:[a-f0-9]{64}", result["image"]
                    ):
                        raise ValueError("Invalid image digest.")
                except (ValueError, KeyError, TypeError) as exc:
                    finish_execution(
                        attempt.pk, error=f"Could not recover image build result: {exc}"
                    )
                    return None
                ContainerExecution.objects.filter(pk=attempt.pk).update(
                    build_log=result.get("log", "")[-8000:],
                    build_finished_at=completed,
                )
                if attempt.training_job.cancel_requested_at or not result["success"]:
                    error = result.get("error", "")
                    if not result["success"] and result.get("log"):
                        error += "\n" + result["log"]
                    finish_execution(attempt.pk, completed=completed, error=error)
                    return None
                with transaction.atomic():
                    job = TrainingJob.objects.select_for_update().get(
                        pk=attempt.training_job_id
                    )
                    if job.cancel_requested_at:
                        finish_execution(attempt.pk)
                    else:
                        ContainerExecution.objects.filter(
                            pk=attempt.pk, state="building"
                        ).update(state="starting", image_digest=result["image"])
                        job.status = "running"
                        job.save(update_fields=["status"])
                return None
            if attempt.training_job.cancel_requested_at:
                finish_execution(attempt.pk)
                return None
            if attempt.build_attempts >= 3:
                finish_execution(
                    attempt.pk,
                    error="Image builder was interrupted three times. Retry the job to build again.",
                )
                return None
            if not attempt.training_job.dockerfile:
                finish_execution(
                    attempt.pk,
                    error="This job has no saved Dockerfile. Submit a new job.",
                )
                return None
            # No living builder or complete result: restart from the immutable
            # snapshot with a new token so stale files can never become this result.
            if directory and (directory / "context").exists():
                shutil.rmtree(directory / "context")
            attempt.build_token = uuid.uuid4()
            attempt.build_attempts += 1
            attempt.build_started_at = timezone.now()
            attempt.save(
                update_fields=["build_token", "build_attempts", "build_started_at"]
            )
            directory = build_directory(attempt)
            directory.mkdir(parents=True)
            project = attempt.training_job.project
            write_json(
                directory / "request.json",
                {
                    "project": {
                        "storage_path": project.storage_path,
                        "resolved_commit": project.resolved_commit,
                    },
                    "dockerfile": attempt.training_job.dockerfile,
                    "tag": f"training-job:{attempt.pk.hex}-{attempt.build_token.hex}",
                },
            )
            try:
                return subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "backend.build_image",
                        str(directory),
                        "--timeout",
                        str(settings.TRAINING_BUILD_TIMEOUT),
                        "--lock-fd",
                        str(build_lock.fileno()),
                    ],
                    cwd=settings.BASE_DIR,
                    pass_fds=(build_lock.fileno(),),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                finish_execution(
                    attempt.pk, error=f"Could not start image builder: {exc}"
                )
                return None
            # Closing the parent's FD does not unlock the child's inherited FD.
