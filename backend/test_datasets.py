import io
import subprocess
import tempfile
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from backend.models import ContainerExecution, Dataset, Project, TrainingJob
from backend.services.datasets import cleanup_datasets, dataset_path, upload_dataset
from backend.services.executions import cancel_job, reconcile_execution, start_job
from backend.services.training import submit_training
from training_server.executor import ContainerNotFound


class DatasetTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        override = self.settings(
            DATASET_STORAGE_ROOT=self.root / "datasets",
            TRAINING_OUTPUT_ROOT=self.root / "outputs",
        )
        override.enable()
        self.addCleanup(override.disable)
        self.project = Project.objects.create(name="Example", status="ready")

    def upload(self, content=b"a,b\n1,2", name="data.csv"):
        return upload_dataset(self.project.pk, SimpleUploadedFile(name, content))

    def job(self):
        dataset = self.upload()
        dataset.expires_at = None
        dataset.save()
        return TrainingJob.objects.create(
            project=self.project, entrypoint="train.py", dataset=dataset
        )

    def docker(self, status="exited", code=0):
        docker = Mock()
        docker.create.return_value = uuid.uuid4().hex * 2
        docker.inspect.return_value = {
            "Id": docker.create.return_value,
            "Image": "sha256:example",
            "State": {
                "Status": status,
                "ExitCode": code,
                "FinishedAt": timezone.now().isoformat(),
            },
        }
        docker.inspect.side_effect = lambda container: {
            **docker.inspect.return_value,
            "Id": container if len(container) == 64 else docker.create.return_value,
        }
        return docker

    def test_upload_api_and_nested_zip(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("images/train/cat.txt", "cat")
            zipped.writestr("labels/train.csv", "cat,1")
        response = self.client.post(
            f"/api/projects/{self.project.pk}/datasets",
            {"file": SimpleUploadedFile("data.zip", archive.getvalue())},
        )
        self.assertEqual(response.status_code, 201, response.content)
        dataset = Dataset.objects.get()
        self.assertEqual(
            (dataset_path(dataset) / "images/train/cat.txt").read_text(), "cat"
        )
        self.assertNotIn("storage_path", response.json())
        self.assertIsNotNone(dataset.expires_at)

    def test_unsafe_archives_and_limits_leave_no_payload(self):
        for name in ["../escape", "/absolute", "folder/../../escape", "folder\\escape"]:
            with self.subTest(name=name):
                archive = io.BytesIO()
                with zipfile.ZipFile(archive, "w") as zipped:
                    zipped.writestr(name, "bad")
                with self.assertRaises(ValueError):
                    self.upload(archive.getvalue(), "bad.zip")
        with self.settings(DATASET_UPLOAD_MAX_BYTES=2), self.assertRaises(ValueError):
            self.upload()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("big", "12345")
        with self.settings(DATASET_EXTRACT_MAX_BYTES=2), self.assertRaises(ValueError):
            self.upload(archive.getvalue(), "big.zip")
        self.assertFalse(Dataset.objects.exists())
        self.assertEqual(list((self.root / "datasets").iterdir()), [])

    def test_submission_claims_once_and_checks_project_and_expiry(self):
        dataset = self.upload()
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
            other = Project.objects.create(name="Other", status="ready")
            with self.assertRaises(ValueError):
                submit_training(other.pk, "train.py", 1, dataset.pk)
            job = submit_training(self.project.pk, "train.py", 1, dataset.pk)
            dataset.refresh_from_db()
            self.assertIsNone(dataset.expires_at)
            self.assertEqual(job.dataset_id, dataset.pk)
            with self.assertRaises(ValueError):
                submit_training(self.project.pk, "train.py", 1, dataset.pk)
            expired = self.upload()
            expired.expires_at = timezone.now() - timedelta(seconds=1)
            expired.save()
            with self.assertRaises(ValueError):
                submit_training(self.project.pk, "train.py", 1, expired.pk)

    def test_completion_and_failure_start_ttl_once(self):
        for code, status in [(0, "finished"), (1, "failed")]:
            with self.subTest(code=code):
                job = self.job()
                docker = self.docker(code=code)
                attempt = start_job(job.pk, "training:test", docker)
                job.refresh_from_db()
                job.dataset.refresh_from_db()
                self.assertEqual(job.status, status)
                self.assertEqual(
                    job.dataset.expires_at, job.finished_at + timedelta(days=1)
                )
                expiry = job.dataset.expires_at
                reconcile_execution(attempt.pk, docker)
                job.dataset.refresh_from_db()
                self.assertEqual(job.dataset.expires_at, expiry)
                self.assertEqual(
                    docker.create.call_args.kwargs["dataset"], dataset_path(job.dataset)
                )
                self.assertEqual(cleanup_datasets(), 0)
                with patch(
                    "backend.services.datasets.timezone.now", return_value=expiry
                ):
                    self.assertEqual(cleanup_datasets(), 1)
                    self.assertEqual(cleanup_datasets(), 0)
                self.assertFalse(dataset_path(job.dataset).exists())
                self.assertTrue(Path(attempt.output_path).exists())
                self.assertTrue(TrainingJob.objects.filter(pk=job.pk).exists())

    def test_retry_retains_data_and_rejects_expired(self):
        job = self.job()
        first = start_job(job.pk, "training:test", self.docker(code=1))
        second = start_job(job.pk, "training:test", self.docker(status="running"))
        self.assertNotEqual(first.pk, second.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "running")
        self.assertIsNone(job.finished_at)
        self.assertIsNone(job.dataset.expires_at)
        self.assertEqual(cleanup_datasets(), 0)
        with self.assertRaises(ValueError):
            start_job(job.pk, "training:test", self.docker())
        reconcile_execution(second.pk, self.docker())
        Dataset.objects.filter(pk=job.dataset_id).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        with self.assertRaises(ValueError):
            start_job(job.pk, "training:test", self.docker())

    def test_cancel_queued_and_running(self):
        job = self.job()
        cancel_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(job.dataset.expires_at, job.finished_at + timedelta(days=1))
        job = self.job()
        start_job(job.pk, "training:test", self.docker(status="running"))
        docker = self.docker()
        running = self.docker(status="running").inspect.return_value
        docker.inspect.side_effect = [running, docker.inspect.return_value]
        cancel_job(job.pk, docker)
        docker.stop.assert_called_once()
        job.refresh_from_db()
        self.assertEqual(job.status, "cancelled")
        self.assertIsNotNone(job.dataset.expires_at)

    def test_orphan_cleanup_and_active_job_guard(self):
        orphan = self.upload()
        job = self.job()
        Dataset.objects.all().update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(cleanup_datasets(), 1)
        self.assertFalse(dataset_path(orphan).exists())
        self.assertTrue(dataset_path(job.dataset).exists())

    def test_cleanup_failure_is_retryable(self):
        dataset = self.upload()
        Dataset.objects.filter(pk=dataset.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        with (
            patch(
                "backend.services.datasets.shutil.rmtree", side_effect=OSError("busy")
            ),
            self.assertRaises(OSError),
        ):
            cleanup_datasets()
        dataset.refresh_from_db()
        self.assertIsNone(dataset.deleted_at)
        self.assertEqual(cleanup_datasets(), 1)

    def test_docker_start_failure_and_uncertain_state(self):
        job = self.job()
        docker = self.docker()
        docker.create.side_effect = ValueError("Bad image")
        docker.inspect.side_effect = ContainerNotFound("No container")
        with self.assertRaises(ValueError):
            start_job(job.pk, "bad", docker)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertIsNotNone(job.dataset.expires_at)
        job = self.job()
        docker.inspect.side_effect = subprocess.TimeoutExpired("docker", 60)
        with self.assertRaises(ValueError):
            start_job(job.pk, "bad", docker)
        job.refresh_from_db()
        self.assertEqual(job.status, "running")
        self.assertIsNone(job.dataset.expires_at)

    def test_job_api_exposes_retention_without_server_path(self):
        job = self.job()
        response = self.client.get(
            f"/api/projects/{self.project.pk}/training-jobs/{job.pk}"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "queued")
        self.assertEqual(response.json()["dataset"]["name"], "data.csv")
        self.assertNotIn(str(self.root), response.content.decode())

    def test_delayed_monitoring_uses_actual_completion_time(self):
        job = self.job()
        docker = self.docker()
        finished = timezone.now() - timedelta(days=2)
        docker.inspect.return_value["State"]["FinishedAt"] = finished.isoformat()
        start_job(job.pk, "training:test", docker)
        job.refresh_from_db()
        self.assertEqual(job.finished_at, finished)
        self.assertEqual(job.dataset.expires_at, finished + timedelta(days=1))
        self.assertEqual(cleanup_datasets(), 1)

    def test_failed_container_start_becomes_failed_without_deleting_data_early(self):
        job = self.job()
        docker = self.docker(status="created")
        docker.start.side_effect = subprocess.CalledProcessError(1, "docker start")
        with self.assertRaises(subprocess.CalledProcessError):
            start_job(job.pk, "training:test", docker)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertEqual(cleanup_datasets(), 0)

    def test_interrupted_start_is_recovered_after_grace_period(self):
        job = self.job()
        job.status = "running"
        job.save()
        attempt = ContainerExecution.objects.create(training_job=job)
        docker = self.docker()
        docker.inspect.side_effect = ContainerNotFound("No container")
        reconcile_execution(attempt.pk, docker)
        job.refresh_from_db()
        self.assertEqual(job.status, "running")
        ContainerExecution.objects.filter(pk=attempt.pk).update(
            created_at=timezone.now() - timedelta(minutes=6)
        )
        reconcile_execution(attempt.pk, docker)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertIsNotNone(job.dataset.expires_at)

    def test_zip_symlink_is_rejected(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            entry = zipfile.ZipInfo("link")
            entry.create_system = 3
            entry.external_attr = 0o120777 << 16
            zipped.writestr(entry, "/etc/passwd")
        with self.assertRaises(ValueError):
            self.upload(archive.getvalue(), "symlink.zip")
        self.assertFalse(Dataset.objects.exists())
