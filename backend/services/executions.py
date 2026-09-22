"""Claim GPUs, launch saved jobs, and reconcile Docker with durable job state."""

import subprocess
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from backend.models import ContainerExecution, Dataset, TrainingJob
from backend.services.datasets import dataset_path
from backend.services.worker_locks import attempt_lock
from training_server.executor import ContainerNotFound, DockerExecutor

ACTIVE = ["building", "starting", "running"]
DOCKER_ERRORS = (OSError, ValueError, subprocess.SubprocessError)


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
    if state["Status"] in ["running", "restarting", "paused"]:
        started = parse_datetime(state.get("StartedAt", ""))
        if started is None or started.year < 2000:
            started = timezone.now()
        ContainerExecution.objects.filter(pk=attempt.pk).update(
            state="running", started_at=attempt.started_at or started
        )
    elif state["Status"] in ["exited", "dead"]:
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
