"""Build, run, and reconcile training executions with durable job state."""

import fcntl
import json
import logging
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError, IntegrityError, close_old_connections, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from backend.build_image import write_json
from backend.models import ContainerExecution, Dataset, TrainingJob
from backend.services.projects import cleanup_datasets, dataset_path
from training_server.executor import ContainerNotFound, DockerExecutor

ACTIVE = ["building", "starting", "running"]
DOCKER_ERRORS = (OSError, ValueError, subprocess.SubprocessError)


def list_executions():
    """Return execution attempts across projects, newest first."""
    return (
        ContainerExecution.objects.select_related("training_job__project")
        .annotate(
            project_name=F("training_job__project__name"),
            entrypoint=F("training_job__entrypoint"),
        )
        .order_by("-created_at", "-id")
    )


class GPUUnavailable(ValueError):
    pass


def resolve_gpu(requested, devices):
    requested = str(requested).strip()
    if requested.isdigit():
        requested = str(int(requested))
    gpu = devices.get(requested, requested)
    if gpu not in devices.values():
        raise ValueError(f"GPU {requested} is not available on this host.")
    return gpu


def hold_dataset(job):
    if not job.dataset_id:
        return
    dataset = Dataset.objects.select_for_update().get(pk=job.dataset_id)
    if (
        dataset.deleted_at
        or (dataset.expires_at and dataset.expires_at <= timezone.now())
        or not dataset_path(dataset).is_dir()
    ):
        raise ValueError(
            "Dataset expired or is unavailable. Submit a new job with a new upload."
        )
    dataset.expires_at = None
    dataset.save(update_fields=["expires_at"])


def reserve_job(job_id, devices, *, image="", queued_only=False):
    """The active execution is the reservation, enforced by database uniqueness."""
    try:
        with transaction.atomic():
            job = TrainingJob.objects.select_for_update().get(pk=job_id)
            if queued_only and job.status != "queued":
                return None
            if (
                job.status in ["building", "running"]
                or job.executions.filter(state__in=ACTIVE).exists()
            ):
                raise ValueError("This job already has an active execution.")
            if job.cancel_requested_at and job.status == "queued":
                return None
            gpu = resolve_gpu(job.requested_gpu, devices)
            occupied = ContainerExecution.objects.filter(state__in=ACTIVE).values_list(
                "assigned_gpu", flat=True
            )
            # Honor reservations from before UUID normalization, too.
            if any(
                not value or resolve_gpu(value, devices) == gpu for value in occupied
            ):
                raise GPUUnavailable(
                    f"GPU {job.requested_gpu} already has an active job."
                )
            hold_dataset(job)
            attempt = ContainerExecution(
                training_job=job,
                assigned_gpu=gpu,
                state="starting" if image else "building",
                image_digest=image,
            )
            attempt.output_path = str(
                Path(settings.TRAINING_OUTPUT_ROOT).resolve()
                / str(job.pk)
                / str(attempt.pk)
            )
            attempt.save()
            job.status = "running" if image else "building"
            job.finished_at = None
            job.cancel_requested_at = None
            job.error = ""
            job.save(
                update_fields=["status", "finished_at", "cancel_requested_at", "error"]
            )
            return attempt
    except IntegrityError as exc:
        # A simultaneous claimant won either the job or the GPU reservation.
        raise GPUUnavailable(
            "The job or its GPU was claimed by another process."
        ) from exc


def _finish_job(job, status, completed, error=""):
    job.status = status
    job.finished_at = completed
    job.error = error[-8000:]
    job.save(update_fields=["status", "finished_at", "error"])
    if job.dataset_id:
        dataset = Dataset.objects.select_for_update().get(pk=job.dataset_id)
        dataset.expires_at = completed + timedelta(days=1)
        dataset.save(update_fields=["expires_at"])


def finish_execution(
    execution_id, *, completed=None, exit_code=None, error="", succeeded=False
):
    """Caller must hold the operation lock and establish that training has stopped."""
    initial = ContainerExecution.objects.get(pk=execution_id)
    with transaction.atomic():
        job = TrainingJob.objects.select_for_update().get(pk=initial.training_job_id)
        attempt = ContainerExecution.objects.select_for_update().get(pk=execution_id)
        if attempt.state not in ACTIVE:
            return attempt
        status = (
            "cancelled"
            if job.cancel_requested_at
            else "finished"
            if succeeded
            else "failed"
        )
        completed = completed or timezone.now()
        attempt.state = "succeeded" if status == "finished" else status
        attempt.finished_at = completed
        attempt.exit_code = exit_code
        attempt.error = error[-8000:]
        attempt.save()
        if job.startup_check and status == "finished":
            status = "checked"
        _finish_job(job, status, completed, error)
        return attempt


def fail_queued_job(job_id, error):
    with transaction.atomic():
        job = TrainingJob.objects.select_for_update().get(pk=job_id)
        if job.status == "queued":
            _finish_job(
                job,
                "cancelled" if job.cancel_requested_at else "failed",
                timezone.now(),
                str(error),
            )


def queue_retry(job_id):
    with transaction.atomic():
        job = TrainingJob.objects.select_for_update().get(pk=job_id)
        if (
            job.status not in ["finished", "failed", "cancelled"]
            or job.executions.filter(state__in=ACTIVE).exists()
        ):
            raise ValueError("Only a completed job can be queued for retry.")
        hold_dataset(job)
        if job.startup_check:
            job.environment_validation = {}
            job.save(update_fields=["environment_validation"])
        job.status, job.error = "queued", ""
        job.finished_at = job.cancel_requested_at = None
        job.save(
            update_fields=["status", "error", "finished_at", "cancel_requested_at"]
        )
    return job


def start_job(job_id, image, executor=None):
    if not image or image.startswith("-"):
        raise ValueError("Supply a built image name.")
    executor = executor or DockerExecutor()
    attempt = reserve_job(job_id, executor.gpu_devices(), image=image)
    if attempt is None:
        raise ValueError("Job cancellation is pending.")
    return reconcile_execution(attempt.pk, executor)


def _reconcile(attempt, executor):
    if attempt.state not in ["starting", "running"]:
        return attempt
    container = attempt.container_id or "job-" + attempt.pk.hex
    job = TrainingJob.objects.get(pk=attempt.training_job_id)
    try:
        info = executor.inspect(container)
    except ContainerNotFound:
        if job.cancel_requested_at:
            return finish_execution(attempt.pk)
        if job.startup_check and job.environment_validation.get("status") in {
            "passed",
            "failed",
        }:
            shutil.rmtree(attempt.output_path, ignore_errors=True)
            report = job.environment_validation
            return finish_execution(
                attempt.pk,
                succeeded=report["status"] == "passed",
                error=""
                if report["status"] == "passed"
                else report.get("output", "Startup failed."),
            )
        if attempt.error or attempt.state == "running" or attempt.container_id:
            return finish_execution(
                attempt.pk,
                error=attempt.error or "Training container no longer exists.",
            )
        if not attempt.image_digest:
            if attempt.created_at > timezone.now() - timedelta(minutes=5):
                return attempt
            return finish_execution(
                attempt.pk, error="Interrupted start has no saved image."
            )
        try:
            output = Path(attempt.output_path)
            output.mkdir(parents=True, exist_ok=True)
            container = executor.create(
                "job-" + attempt.pk.hex,
                attempt.image_digest,
                output,
                [],
                attempt.assigned_gpu,
                dataset=dataset_path(job.dataset) if job.dataset_id else None,
                **(
                    {
                        "dataset_target": job.dataset_target,
                        "dataset_file": job.dataset.name
                        if not job.dataset.name.lower().endswith(".zip")
                        else "",
                    }
                    if job.dataset_id and job.dataset_target
                    else {}
                ),
            )
            ContainerExecution.objects.filter(pk=attempt.pk).update(
                container_id=container
            )
            attempt.container_id = container
            info = executor.inspect(container)
        except DOCKER_ERRORS as exc:
            ContainerExecution.objects.filter(pk=attempt.pk).update(
                error=str(exc)[-8000:]
            )
            # A failed client may still have created a container. Only a confirmed
            # absence releases the reservation; outages leave it protected.
            try:
                executor.inspect("job-" + attempt.pk.hex)
            except ContainerNotFound:
                finish_execution(attempt.pk, error=str(exc))
            except DOCKER_ERRORS:
                pass
            raise
    state = info["State"]
    ContainerExecution.objects.filter(pk=attempt.pk).update(
        container_id=info["Id"], image_digest=info["Image"]
    )
    job.refresh_from_db()
    if state["Status"] == "created":
        if job.cancel_requested_at:
            return finish_execution(attempt.pk)
        try:
            executor.start(container)
        except DOCKER_ERRORS as exc:
            ContainerExecution.objects.filter(pk=attempt.pk).update(
                error=str(exc)[-8000:]
            )
            if (
                isinstance(exc, subprocess.CalledProcessError)
                and executor.inspect(container)["State"]["Status"] == "created"
            ):
                finish_execution(attempt.pk, error=str(exc))
            raise
        state = executor.inspect(container)["State"]
    job.refresh_from_db()
    if job.cancel_requested_at and state["Status"] in [
        "running",
        "restarting",
        "paused",
    ]:
        executor.stop(container)
        state = executor.inspect(container)["State"]
    if job.startup_check and not job.cancel_requested_at:
        started = attempt.started_at or parse_datetime(state.get("StartedAt", ""))
        elapsed = (
            started
            and started.year >= 2000
            and timezone.now() >= started + timedelta(seconds=30)
        )
        exited = state["Status"] in ["exited", "dead"]
        if exited or (
            elapsed and state["Status"] in {"running", "paused", "restarting"}
        ):
            # Persist the result before cleanup so restart recovery preserves it.
            report = job.environment_validation
            if report.get("status") not in {"passed", "failed"}:
                passed = state["Status"] == "running" or (
                    state["Status"] == "exited" and state.get("ExitCode") == 0
                )
                report = {
                    "status": "passed" if passed else "failed",
                    "message": "Started successfully (observed for 30 seconds)."
                    if not exited and passed
                    else "Script completed successfully."
                    if passed
                    else "Script failed during startup.",
                    "output": executor.captured_logs(container),
                    "finished_at": timezone.now().isoformat(),
                }
                job.environment_validation = report
                job.save(update_fields=["environment_validation"])
            if not exited:
                executor.stop(container)
            executor.remove(container)
            shutil.rmtree(attempt.output_path, ignore_errors=True)
            return finish_execution(
                attempt.pk,
                succeeded=report["status"] == "passed",
                error=""
                if report["status"] == "passed"
                else report["message"] + "\n" + report["output"],
            )
    if state["Status"] in ["running", "restarting", "paused"]:
        started = parse_datetime(state.get("StartedAt", ""))
        if started is None or started.year < 2000:
            started = timezone.now()
        ContainerExecution.objects.filter(pk=attempt.pk).update(
            state="running", started_at=attempt.started_at or started
        )
    elif state["Status"] in ["exited", "dead"]:
        executor.capture_project_files(container, Path(attempt.output_path))
        completed = parse_datetime(state.get("FinishedAt", ""))
        if completed is None or completed.year < 2000:
            completed = timezone.now()
        code = state.get("ExitCode")
        return finish_execution(
            attempt.pk,
            completed=completed,
            exit_code=code,
            succeeded=state["Status"] == "exited" and code == 0,
            error=state.get("Error", "")
            or (f"Container exited with code {code}." if code else ""),
        )
    attempt.refresh_from_db()
    return attempt


def reconcile_execution(execution_id, executor=None):
    executor = executor or DockerExecutor()
    with attempt_lock(execution_id) as acquired:
        attempt = ContainerExecution.objects.get(pk=execution_id)
        if not acquired:
            return attempt
        return _reconcile(attempt, executor)


def cancel_job(job_id, executor=None):
    with transaction.atomic():
        job = TrainingJob.objects.select_for_update().get(pk=job_id)
        if job.status not in ["queued", "building", "running"]:
            raise ValueError("This job has already ended.")
        job.cancel_requested_at = timezone.now()
        job.save(update_fields=["cancel_requested_at"])
        attempt = job.executions.filter(state__in=ACTIVE).first()
        if attempt is None:
            _finish_job(job, "cancelled", job.cancel_requested_at)
            return
    # Building cancellation is observed by the worker/build process. No Docker or
    # filesystem access occurs while holding the job's database lock.
    if attempt.state != "building":
        reconcile_execution(attempt.pk, executor)


# Host operation locks


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def attempt_directory(execution_id):
    return Path(settings.TRAINING_WORK_ROOT).resolve() / str(execution_id)


def attempt_lock(execution_id):
    return file_lock(attempt_directory(execution_id) / "operation.lock")


# Image build coordination


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


# Queue worker

logger = logging.getLogger(__name__)


class TrainingWorker:
    def __init__(self, gpus, *, executor=None, cleanup_interval=60):
        self.executor = executor or DockerExecutor()
        self.devices = self.executor.gpu_devices()
        self.gpus = {resolve_gpu(gpu, self.devices) for gpu in gpus}
        if not self.gpus:
            raise ValueError("Configure at least one GPU for the worker.")
        self.cleanup_interval = cleanup_interval
        self.next_cleanup = 0
        self.children = []

    def tick(self, stop=None):
        close_old_connections()
        self.children = [child for child in self.children if child.poll() is None]
        # Reconcile existing work before claiming: an exited container releases its
        # reservation here, while a Docker outage keeps the reservation intact.
        for attempt in ContainerExecution.objects.filter(state__in=ACTIVE).iterator():
            if stop is not None and stop.is_set():
                return
            try:
                if attempt.state == "building":
                    child = poll_build(attempt.pk)
                    if child is not None:
                        self.children.append(child)
                else:
                    reconcile_execution(attempt.pk, self.executor)
            except (OSError, ValueError, subprocess.SubprocessError, DatabaseError):
                logger.exception(
                    "Could not advance execution %s; retaining its reservation",
                    attempt.pk,
                )
        for job in (
            TrainingJob.objects.filter(status="queued")
            .order_by("created_at", "id")
            .iterator()
        ):
            if stop is not None and stop.is_set():
                break
            try:
                gpu = resolve_gpu(job.requested_gpu, self.devices)
                if gpu not in self.gpus:
                    continue
                attempt = reserve_job(job.pk, self.devices, queued_only=True)
                if attempt:
                    child = poll_build(attempt.pk)
                    if child is not None:
                        self.children.append(child)
            except GPUUnavailable:
                continue
            except ValueError as exc:
                fail_queued_job(job.pk, exc)
            except (OSError, subprocess.SubprocessError, DatabaseError):
                logger.exception("Could not claim job %s; will try again", job.pk)
        if time.monotonic() >= self.next_cleanup:
            try:
                cleanup_datasets()
            except (OSError, ValueError, DatabaseError):
                logger.exception("Dataset cleanup failed; will retry on the next sweep")
            self.next_cleanup = time.monotonic() + self.cleanup_interval
