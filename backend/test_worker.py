import fcntl
import io
import json
import subprocess
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from backend.build_image import run_build, write_json
from backend.models import ContainerExecution, Project, TrainingJob
from backend.services.builds import build_directory, poll_build
from backend.services.executions import (
    GPUUnavailable,
    cancel_job,
    queue_retry,
    reconcile_execution,
    reserve_job,
)
from backend.services.worker import TrainingWorker
from backend.services.worker_locks import attempt_directory, file_lock
from training_server.executor import ContainerNotFound

DEVICES = {
    "0": "GPU-00000000-0000-0000-0000-000000000000",
    "1": "GPU-11111111-1111-1111-1111-111111111111",
}
IMAGE = "sha256:" + "a" * 64


class WorkerTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        override = self.settings(
            TRAINING_WORK_ROOT=self.root / "worker",
            TRAINING_OUTPUT_ROOT=self.root / "outputs",
            DATASET_STORAGE_ROOT=self.root / "datasets",
        )
        override.enable()
        self.addCleanup(override.disable)
        self.project = Project.objects.create(
            name="Example",
            status="ready",
            storage_path=str(self.root / "snapshot"),
            resolved_commit="a" * 40,
        )
        self.docker = Mock()
        self.docker.gpu_devices.return_value = DEVICES
        self.docker.inspect.side_effect = ContainerNotFound("Missing")
        self.docker.create.return_value = "b" * 64

    def job(self, gpu="0"):
        return TrainingJob.objects.create(
            project=self.project,
            entrypoint="train.py",
            requested_gpu=gpu,
            dockerfile='FROM python:3.13\nCMD ["python", "/app/train.py"]',
            dockerfile_source="project",
        )

    def result(self, attempt, success=True):
        attempt.refresh_from_db()
        directory = build_directory(attempt)
        write_json(
            directory / "result.json",
            {
                "success": success,
                "image": IMAGE,
                "error": "" if success else "build failed",
                "log": "build output",
                "finished_at": timezone.now().isoformat(),
            },
        )
        return directory

    def test_claim_reserves_job_and_gpu_including_uuid_alias(self):
        first = self.job()
        claimed = reserve_job(first.pk, DEVICES, queued_only=True)
        self.assertEqual(claimed.state, "building")
        self.assertEqual(claimed.assigned_gpu, DEVICES["0"])
        self.assertIsNone(reserve_job(first.pk, DEVICES, queued_only=True))
        second = self.job(DEVICES["0"])
        with self.assertRaises(GPUUnavailable):
            reserve_job(second.pk, DEVICES, queued_only=True)
        other = reserve_job(self.job("1").pk, DEVICES, queued_only=True)
        self.assertEqual(other.assigned_gpu, DEVICES["1"])
        second.refresh_from_db()
        self.assertEqual(second.status, "queued")

    def test_database_constraints_reject_duplicate_reservations(self):
        job = self.job()
        reserve_job(job.pk, DEVICES)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContainerExecution.objects.create(
                training_job=self.job(), state="building", assigned_gpu=DEVICES["0"]
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContainerExecution.objects.create(
                training_job=job, state="running", assigned_gpu=DEVICES["1"]
            )

    def test_worker_claims_oldest_per_gpu_and_leaves_busy_queue(self):
        jobs = [self.job(), self.job(), self.job("1")]
        with patch("backend.services.worker.poll_build") as build:
            worker = TrainingWorker(["0", "1"], executor=self.docker)
            worker.tick()
        self.assertEqual(
            list(
                TrainingJob.objects.order_by("created_at").values_list(
                    "status", flat=True
                )
            ),
            ["building", "queued", "building"],
        )
        self.assertEqual(build.call_count, 2)
        self.assertEqual(
            ContainerExecution.objects.filter(training_job=jobs[1]).count(), 0
        )

    def test_builder_launch_persists_identity_and_uses_inherited_lock(self):
        attempt = reserve_job(self.job().pk, DEVICES)
        with patch("backend.services.builds.subprocess.Popen") as launch:
            poll_build(attempt.pk)
        attempt.refresh_from_db()
        self.assertEqual(attempt.build_attempts, 1)
        self.assertIsNotNone(attempt.build_started_at)
        request = json.loads((build_directory(attempt) / "request.json").read_text())
        self.assertEqual(
            request["project"]["resolved_commit"], self.project.resolved_commit
        )
        self.assertEqual(request["dockerfile"], attempt.training_job.dockerfile)
        self.assertIn("--lock-fd", launch.call_args.args[0])
        self.assertEqual(len(launch.call_args.kwargs["pass_fds"]), 1)

    def test_live_builder_survives_worker_restart_without_duplicate(self):
        attempt = reserve_job(self.job().pk, DEVICES)
        with patch("backend.services.builds.subprocess.Popen"):
            poll_build(attempt.pk)
        attempt.refresh_from_db()
        with (attempt_directory(attempt.pk) / "build.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("backend.services.builds.subprocess.Popen") as launch:
                restarted = TrainingWorker(["0"], executor=self.docker)
                restarted.tick()
                launch.assert_not_called()
        self.assertEqual(ContainerExecution.objects.count(), 1)

    def test_completed_build_is_recovered_and_starts_existing_attempt(self):
        attempt = reserve_job(self.job().pk, DEVICES)
        with patch("backend.services.builds.subprocess.Popen"):
            poll_build(attempt.pk)
        self.result(attempt)
        poll_build(attempt.pk)
        attempt.refresh_from_db()
        self.assertEqual(attempt.image_digest, IMAGE)
        self.assertEqual(attempt.state, "starting")
        self.assertEqual(attempt.build_log, "build output")
        created = {"Id": "b" * 64, "Image": IMAGE, "State": {"Status": "created"}}
        running = {**created, "State": {"Status": "running"}}
        self.docker.inspect.side_effect = [
            ContainerNotFound("Missing"),
            created,
            running,
        ]
        reconcile_execution(attempt.pk, self.docker)
        self.assertEqual(self.docker.create.call_args.args[1], IMAGE)
        self.assertEqual(self.docker.create.call_args.args[4], DEVICES["0"])
        self.docker.start.assert_called_once()
        self.assertEqual(ContainerExecution.objects.count(), 1)
        # A restarted worker inspects the existing container instead of recreating it.
        self.docker.inspect.side_effect = None
        self.docker.inspect.return_value = running
        reconcile_execution(attempt.pk, self.docker)
        self.assertEqual(self.docker.create.call_count, 1)

    def test_build_failure_releases_gpu_and_retry_requeues(self):
        job = self.job()
        attempt = reserve_job(job.pk, DEVICES)
        with patch("backend.services.builds.subprocess.Popen"):
            poll_build(attempt.pk)
        self.result(attempt, success=False)
        poll_build(attempt.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertIn("build output", job.error)
        queue_retry(job.pk)
        retried = reserve_job(job.pk, DEVICES, queued_only=True)
        self.assertNotEqual(attempt.pk, retried.pk)

    def test_interrupted_build_restarts_with_new_token_then_stops_after_limit(self):
        job = self.job()
        attempt = reserve_job(job.pk, DEVICES)
        tokens = []
        with patch("backend.services.builds.subprocess.Popen") as launch:
            for _ in range(3):
                poll_build(attempt.pk)
                attempt.refresh_from_db()
                tokens.append(attempt.build_token)
            poll_build(attempt.pk)
        self.assertEqual(len(set(tokens)), 3)
        self.assertEqual(launch.call_count, 3)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertIn("interrupted", job.error)

    def test_cancellation_signals_live_builder_and_never_starts_training(self):
        job = self.job()
        attempt = reserve_job(job.pk, DEVICES)
        with patch("backend.services.builds.subprocess.Popen"):
            poll_build(attempt.pk)
        attempt.refresh_from_db()
        with (attempt_directory(attempt.pk) / "build.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            cancel_job(job.pk, self.docker)
            poll_build(attempt.pk)
            self.assertTrue((build_directory(attempt) / "cancel").exists())
            job.refresh_from_db()
            self.assertEqual(job.status, "building")
        # Even if completion raced with cancellation, its image must not run.
        self.result(attempt)
        poll_build(attempt.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "cancelled")
        self.docker.create.assert_not_called()

    def test_outage_keeps_reservation_and_does_not_claim_next_job(self):
        job = self.job()
        reserve_job(job.pk, DEVICES, image=IMAGE)
        next_job = self.job()
        self.docker.inspect.side_effect = subprocess.TimeoutExpired("docker", 60)
        with self.assertLogs("backend.services.worker", level="ERROR"):
            TrainingWorker(["0"], executor=self.docker).tick()
        next_job.refresh_from_db()
        self.assertEqual(next_job.status, "queued")
        self.assertEqual(ContainerExecution.objects.count(), 1)

    def test_singleton_worker_command_and_once(self):
        with file_lock(self.root / "worker" / "worker.lock"):
            with self.assertRaisesMessage(CommandError, "already running"):
                call_command("training_worker", once=True)
        with patch(
            "backend.management.commands.training_worker.TrainingWorker"
        ) as worker:
            call_command("training_worker", once=True, stdout=io.StringIO())
            worker.return_value.tick.assert_called_once()

    def test_empty_queue_still_cleans_up(self):
        with patch("backend.services.worker.cleanup_datasets") as cleanup:
            worker = TrainingWorker(["0"], executor=self.docker)
            worker.tick()
            worker.tick()
            cleanup.assert_called_once()

    def test_builder_records_digest_and_failures_without_database_transactions(self):
        directory = self.root / "build"
        directory.mkdir()
        write_json(
            directory / "request.json",
            {
                "project": {"storage_path": "snapshot", "resolved_commit": "commit"},
                "dockerfile": "FROM example",
                "tag": "training:test",
            },
        )

        def launch(command, **kwargs):
            self.assertEqual(command[:2], ["docker", "build"])
            self.assertEqual(
                (directory / "context" / "Dockerfile").read_text(), "FROM example"
            )
            (directory / "image-id").write_text(IMAGE)
            process = Mock()
            process.poll.return_value = 0
            process.returncode = 0
            return process

        with (
            patch("backend.services.environment_validation.export_snapshot") as export,
            patch("backend.build_image.subprocess.Popen", side_effect=launch),
        ):
            result = run_build(directory, 10)
        self.assertEqual(result["image"], IMAGE)
        self.assertEqual(export.call_args.args[0].resolved_commit, "commit")
        self.assertFalse((directory / "context").exists())
        self.assertEqual(
            json.loads((directory / "result.json").read_text())["success"], True
        )

    def test_builder_cancellation_and_timeout_stop_client(self):
        for reason in ["cancel", "timeout"]:
            with self.subTest(reason=reason):
                directory = self.root / reason
                directory.mkdir()
                write_json(
                    directory / "request.json",
                    {"project": {}, "dockerfile": "FROM example", "tag": "test"},
                )
                process = Mock()
                process.poll.return_value = None

                def launch(*args, **kwargs):
                    if reason == "cancel":
                        (directory / "cancel").touch()
                    return process

                with (
                    patch("backend.services.environment_validation.export_snapshot"),
                    patch("backend.build_image.subprocess.Popen", side_effect=launch),
                ):
                    result = run_build(directory, -1)
                self.assertFalse(result["success"])
                self.assertIn(
                    "cancelled" if reason == "cancel" else "timed out", result["error"]
                )
                process.terminate.assert_called_once()

    def test_real_child_keeps_build_lock_after_launching_worker_returns(self):
        attempt = reserve_job(self.job().pk, DEVICES)
        real_popen = subprocess.Popen
        script = (
            "import sys,json,pathlib,datetime; "
            "directory=pathlib.Path(sys.argv[1]); "
            "sys.stdin.readline(); "
            'result={"success":True,"image":sys.argv[2],"log":"recovered child",'
            '"finished_at":datetime.datetime.now(datetime.timezone.utc).isoformat()}; '
            '(directory/"result.json").write_text(json.dumps(result))'
        )

        def launch(command, **kwargs):
            return real_popen(
                [command[0], "-c", script, command[3], IMAGE],
                pass_fds=kwargs["pass_fds"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        with patch(
            "backend.services.builds.subprocess.Popen", side_effect=launch
        ) as mocked:
            child = poll_build(attempt.pk)
            try:
                self.assertIsNone(poll_build(attempt.pk))
                self.assertEqual(mocked.call_count, 1)
                _, stderr = child.communicate(b"finish\n", timeout=5)
                self.assertEqual(child.returncode, 0, stderr)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate()
        poll_build(attempt.pk)
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, "starting")
        self.assertEqual(attempt.build_log, "recovered child")

    def test_cancelled_queued_job_is_never_claimed(self):
        job = self.job()
        cancel_job(job.pk, self.docker)
        TrainingWorker(["0"], executor=self.docker).tick()
        self.assertFalse(ContainerExecution.objects.exists())

    def test_builder_export_failure_is_a_durable_failure(self):
        directory = self.root / "failed-export"
        directory.mkdir()
        write_json(
            directory / "request.json",
            {"project": {}, "dockerfile": "FROM example", "tag": "test"},
        )
        with patch(
            "backend.services.environment_validation.export_snapshot",
            side_effect=ValueError("Unsafe snapshot"),
        ):
            result = run_build(directory, 10)
        self.assertFalse(result["success"])
        self.assertIn("Unsafe snapshot", result["error"])
        self.assertTrue((directory / "result.json").exists())

    def test_shutdown_signal_prevents_new_claims(self):
        job = self.job()
        stop = threading.Event()
        stop.set()
        with patch("backend.services.worker.poll_build") as build:
            TrainingWorker(["0"], executor=self.docker).tick(stop=stop)
            build.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        self.assertFalse(ContainerExecution.objects.exists())


class WorkerTransactionTests(TransactionTestCase):
    def test_docker_and_builder_launch_do_not_hold_database_transactions(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            self.settings(
                TRAINING_WORK_ROOT=Path(directory) / "work",
                TRAINING_OUTPUT_ROOT=Path(directory) / "outputs",
            ),
        ):
            project = Project.objects.create(
                name="Example",
                status="ready",
                storage_path="snapshot",
                resolved_commit="a" * 40,
            )
            job = TrainingJob.objects.create(
                project=project, entrypoint="train.py", dockerfile="FROM example"
            )
            attempt = reserve_job(job.pk, DEVICES)

            def launch(*args, **kwargs):
                self.assertFalse(connection.in_atomic_block)
                return Mock()

            with patch("backend.services.builds.subprocess.Popen", side_effect=launch):
                poll_build(attempt.pk)
            attempt.refresh_from_db()
            write_json(
                build_directory(attempt) / "result.json",
                {
                    "success": True,
                    "image": IMAGE,
                    "log": "",
                    "finished_at": timezone.now().isoformat(),
                },
            )
            poll_build(attempt.pk)
            docker = Mock()
            exists = [False]

            def inspect(container):
                self.assertFalse(connection.in_atomic_block)
                if not exists[0]:
                    raise ContainerNotFound("Missing")
                return {"Id": "b" * 64, "Image": IMAGE, "State": {"Status": "running"}}

            def create(*args, **kwargs):
                self.assertFalse(connection.in_atomic_block)
                exists[0] = True
                return "b" * 64

            docker.inspect.side_effect = inspect
            docker.create.side_effect = create
            reconcile_execution(attempt.pk, docker)
            job.refresh_from_db()
            self.assertEqual(job.status, "running")
