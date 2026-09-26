"""Startup checks gate submission and clean up without losing GPU reservations."""

import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from backend.models import Dataset, Project, TrainingJob
from backend.services.datasets import dataset_path
from backend.services.executions import reconcile_execution, reserve_job
from backend.services.training import confirm_training, submit_training
from training_server.executor import ContainerNotFound, DockerExecutor


class StartupTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        override = self.settings(
            TRAINING_WORK_ROOT=self.root / "work",
            TRAINING_OUTPUT_ROOT=self.root / "output",
            DATASET_STORAGE_ROOT=self.root / "data",
        )
        override.enable()
        self.addCleanup(override.disable)
        self.project = Project.objects.create(name="Example", status="ready")
        self.job = TrainingJob.objects.create(
            project=self.project, entrypoint="train.py", startup_check=True
        )
        self.attempt = reserve_job(self.job.pk, {"0": "GPU-aaaa"}, image="image")
        self.docker = Mock()
        self.docker.captured_logs.return_value = "Loaded dataset\n"
        self.docker.inspect.return_value = {
            "Id": "b" * 64,
            "Image": "image",
            "State": {
                "Status": "running",
                "StartedAt": (timezone.now() - timedelta(seconds=31)).isoformat(),
            },
        }

    def test_running_check_stops_and_requires_explicit_submission(self):
        reconcile_execution(self.attempt.pk, self.docker)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "checked")
        self.assertEqual(self.job.environment_validation["output"], "Loaded dataset\n")
        self.docker.stop.assert_called_once()
        self.docker.remove.assert_called_once()
        submitted = confirm_training(self.project.pk, self.job.pk)
        self.assertFalse(submitted.startup_check)
        self.assertEqual(submitted.status, "queued")
        with self.assertRaises(ValueError):
            confirm_training(self.project.pk, self.job.pk)

    def test_failed_startup_blocks_submission_and_shows_logs(self):
        self.docker.inspect.return_value["State"] = {"Status": "exited", "ExitCode": 1}
        self.docker.captured_logs.return_value = "FileNotFoundError: data/train.csv"
        reconcile_execution(self.attempt.pk, self.docker)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "failed")
        self.assertIn("FileNotFoundError", self.job.error)
        with self.assertRaises(ValueError):
            confirm_training(self.project.pk, self.job.pk)

    def test_quick_success_is_accepted_but_young_process_is_not(self):
        self.docker.inspect.return_value["State"]["StartedAt"] = (
            timezone.now().isoformat()
        )
        reconcile_execution(self.attempt.pk, self.docker)
        self.docker.stop.assert_not_called()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "running")
        self.docker.inspect.return_value["State"] = {"Status": "exited", "ExitCode": 0}
        reconcile_execution(self.attempt.pk, self.docker)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "checked")

    def test_cleanup_outage_retains_reservation_and_recovers_result(self):
        self.docker.remove.side_effect = OSError("Docker unavailable")
        with self.assertRaises(OSError):
            reconcile_execution(self.attempt.pk, self.docker)
        self.attempt.refresh_from_db()
        self.assertIn(self.attempt.state, ["starting", "running"])
        self.docker.inspect.side_effect = ContainerNotFound("Removed")
        reconcile_execution(self.attempt.pk, self.docker)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "checked")

    def test_expired_dataset_cannot_be_submitted(self):
        dataset = Dataset.objects.create(
            project=self.project,
            name="train.csv",
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.job.dataset = dataset
        self.job.status = "checked"
        self.job.save()
        with self.assertRaises(ValueError):
            confirm_training(self.project.pk, self.job.pk)

    def test_paths_and_optional_arguments(self):
        dataset = Dataset.objects.create(project=self.project, name="train.csv")
        dataset_path(dataset).mkdir(parents=True)
        with (
            patch(
                "backend.services.training.list_project_files",
                return_value={"entries": [{"name": "train.py", "type": "file"}]},
            ),
            patch(
                "backend.services.training.prepare_dockerfile",
                return_value=("FROM example", "project"),
            ),
        ):
            for target in [
                "../outside",
                "/absolute",
                ".",
                "train.py",
                "data,other",
                "data\\file",
            ]:
                with self.subTest(target=target), self.assertRaises(ValueError):
                    submit_training(
                        self.project.pk,
                        "train.py",
                        dataset_id=dataset.pk,
                        dataset_target=target,
                        startup_check=True,
                    )
            job = submit_training(
                self.project.pk,
                "train.py",
                dataset_id=dataset.pk,
                dataset_target="data/train.csv",
                startup_check=True,
            )
        self.assertEqual(job.arguments, [])
        self.assertEqual(job.dataset_target, "data/train.csv")
        self.assertTrue(job.startup_check)

    def test_executor_mounts_single_file_and_zip_directory(self):
        payload = self.root / "payload"
        payload.mkdir()
        (payload / "uploaded.csv").write_text("data")
        executor = DockerExecutor()
        for filename, target, source in [
            ("uploaded.csv", "data/train.csv", payload / "uploaded.csv"),
            ("", "data/images", payload),
        ]:
            with (
                self.subTest(target=target),
                patch.object(executor, "_run", return_value="container") as run,
            ):
                executor.create(
                    "job-" + "a" * 32,
                    "image",
                    self.root,
                    [],
                    dataset=payload,
                    dataset_target=target,
                    dataset_file=filename,
                )
                self.assertIn(
                    f"type=bind,src={source},dst=/app/{target},readonly",
                    run.call_args.args,
                )

    def test_api_requires_success_and_scopes_confirmation_to_project(self):
        url = f"/api/projects/{self.project.pk}/training-jobs/{self.job.pk}/submit"
        self.assertEqual(self.client.post(url).status_code, 400)
        other = Project.objects.create(name="Other")
        self.assertEqual(
            self.client.post(
                f"/api/projects/{other.pk}/training-jobs/{self.job.pk}/submit"
            ).status_code,
            404,
        )
        reconcile_execution(self.attempt.pk, self.docker)
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["startup_check"])
        self.assertEqual(response.json()["status"], "queued")
