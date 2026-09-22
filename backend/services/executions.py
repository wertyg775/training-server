"""Connect saved jobs to Docker attempts and dataset retention."""

import subprocess
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from backend.models import ContainerExecution, Dataset, TrainingJob
from backend.services.datasets import dataset_path
from training_server.executor import ContainerNotFound, DockerExecutor

ACTIVE = [ContainerExecution.State.STARTING, ContainerExecution.State.RUNNING]


def start_job(job_id, image, executor=None):
    executor = executor or DockerExecutor()
    with transaction.atomic():
        job = TrainingJob.objects.select_for_update().get(pk=job_id)
        if (
            job.status == TrainingJob.Status.RUNNING
            or job.executions.filter(state__in=ACTIVE).exists()
        ):
            raise ValueError("This job already has an active execution.")
        data_path = None
        if job.dataset_id:
            dataset = Dataset.objects.select_for_update().get(pk=job.dataset_id)
            if (
                dataset.deleted_at
                or (dataset.expires_at and dataset.expires_at <= timezone.now())
                or not dataset_path(dataset).is_dir()
            ):
                raise ValueError(
                    "Dataset expired or is unavailable. Submit a new job with a new upload."
                )
            data_path = dataset_path(dataset)
            dataset.expires_at = None
            dataset.save(update_fields=["expires_at"])
        attempt = ContainerExecution(training_job=job, assigned_gpu=job.requested_gpu)
        output = (
            Path(settings.TRAINING_OUTPUT_ROOT).resolve()
            / str(job.pk)
            / str(attempt.pk)
        )
        output.mkdir(parents=True, exist_ok=False)
        attempt.output_path = str(output)
        attempt.save()
        job.status = TrainingJob.Status.RUNNING
        job.finished_at = None
        job.cancel_requested_at = None
        job.save(update_fields=["status", "finished_at", "cancel_requested_at"])
    # Commit the identity and retention hold before contacting Docker. A worker crash
    # can then be recovered by inspecting the deterministic container name.
    failure = None
    with transaction.atomic():
        job = TrainingJob.objects.select_for_update().get(pk=job_id)
        attempt.refresh_from_db()
        if attempt.state not in ACTIVE or job.cancel_requested_at:
            return reconcile_execution(attempt.pk, executor)
        try:
            container = executor.create(
                "job-" + attempt.pk.hex,
                image,
                output,
                [],
                job.requested_gpu,
                dataset=data_path,
            )
            attempt.container_id = container
            attempt.save(update_fields=["container_id"])
            executor.start(container)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            attempt.error = str(exc)
            attempt.save(update_fields=["error"])
            failure = exc
    # Preserve the attempt even when Docker fails; reconciliation determines whether
    # a timed-out client actually created a container before scheduling deletion.
    try:
        attempt = reconcile_execution(attempt.pk, executor)
    except (OSError, ValueError, subprocess.SubprocessError):
        if failure is None:
            raise
    if failure:
        raise failure

    return attempt


def reconcile_execution(execution_id, executor=None):
    executor = executor or DockerExecutor()
    initial = ContainerExecution.objects.get(pk=execution_id)
    with transaction.atomic():
        job = TrainingJob.objects.select_for_update().get(pk=initial.training_job_id)
        attempt = ContainerExecution.objects.select_for_update().get(pk=execution_id)
        if attempt.state not in ACTIVE:
            return attempt
        container = attempt.container_id or "job-" + attempt.pk.hex
        try:
            info = executor.inspect(container)
        except ContainerNotFound:
            if (
                attempt.container_id is None
                and not attempt.error
                and attempt.created_at > timezone.now() - timedelta(minutes=5)
                and not job.cancel_requested_at
            ):
                # A just-claimed attempt may still be about to call Docker create.
                return attempt
            # Docker confirms there is no container left that can use this dataset.
            info = {
                "State": {"Status": "dead", "ExitCode": None},
                "Id": attempt.container_id,
                "Image": attempt.image_digest,
            }
        state = info["State"]
        attempt.container_id = info["Id"]
        attempt.image_digest = info["Image"]
        if state["Status"] == "created":
            if job.cancel_requested_at:
                # A created container has never run and cannot still be reading data.
                state = {"Status": "exited", "ExitCode": None}
            else:
                try:
                    executor.start(container)
                except subprocess.CalledProcessError as exc:
                    # A failed start can still have changed Docker state. Inspect
                    # again before treating it as a terminal failure.
                    state = executor.inspect(container)["State"]
                    if state["Status"] == "created":
                        attempt.error = str(exc)
                        state = {"Status": "dead", "ExitCode": None}
                else:
                    attempt.save()
                    return attempt
        if job.cancel_requested_at and state["Status"] in (
            "running",
            "restarting",
            "paused",
        ):
            executor.stop(container)
            state = executor.inspect(container)["State"]
        if state["Status"] in ("running", "restarting", "paused"):
            attempt.state = ContainerExecution.State.RUNNING
            attempt.started_at = attempt.started_at or timezone.now()
        elif state["Status"] in ("exited", "dead"):
            completed = parse_datetime(state.get("FinishedAt", ""))
            if completed is None or completed.year < 2000:
                completed = timezone.now()
            attempt.finished_at = completed
            attempt.exit_code = state.get("ExitCode")
            if job.cancel_requested_at:
                attempt.state = ContainerExecution.State.CANCELLED
                job.status = TrainingJob.Status.CANCELLED
            elif state["Status"] == "exited" and attempt.exit_code == 0:
                attempt.state = ContainerExecution.State.SUCCEEDED
                job.status = TrainingJob.Status.FINISHED
            else:
                attempt.state = ContainerExecution.State.FAILED
                job.status = TrainingJob.Status.FAILED
            job.finished_at = completed
            job.save(update_fields=["status", "finished_at"])
            if job.dataset_id:
                dataset = Dataset.objects.select_for_update().get(pk=job.dataset_id)
                dataset.expires_at = completed + timedelta(days=1)
                dataset.save(update_fields=["expires_at"])
        attempt.save()
        return attempt


def cancel_job(job_id, executor=None):
    executor = executor or DockerExecutor()
    with transaction.atomic():
        job = TrainingJob.objects.select_for_update().get(pk=job_id)
        if job.status not in [TrainingJob.Status.QUEUED, TrainingJob.Status.RUNNING]:
            raise ValueError("This job has already ended.")
        job.cancel_requested_at = timezone.now()
        job.save(update_fields=["cancel_requested_at"])
        attempt = job.executions.filter(state__in=ACTIVE).first()
        if attempt is None:
            job.status = TrainingJob.Status.CANCELLED
            job.finished_at = job.cancel_requested_at
            job.save(update_fields=["status", "finished_at"])
            if job.dataset_id:
                dataset = Dataset.objects.select_for_update().get(pk=job.dataset_id)
                dataset.expires_at = job.finished_at + timedelta(days=1)
                dataset.save(update_fields=["expires_at"])
            return
    reconcile_execution(attempt.pk, executor)
