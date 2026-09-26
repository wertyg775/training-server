import tempfile
from datetime import timedelta
from pathlib import Path

from django.test import TestCase
from django.utils import timezone

from backend.models import ContainerExecution, Project, TrainingJob


class OutputTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        override = self.settings(TRAINING_OUTPUT_ROOT=self.root / "outputs")
        override.enable()
        self.addCleanup(override.disable)
        project = Project.objects.create(name="Example", source_type="upload")
        self.job = TrainingJob.objects.create(
            project=project, entrypoint="src/train.py"
        )

    def execution(self, state="succeeded"):
        attempt = ContainerExecution.objects.create(
            training_job=self.job, state=state, assigned_gpu="GPU-example"
        )
        attempt.output_path = str(
            self.root / "outputs" / str(self.job.pk) / str(attempt.pk)
        )
        attempt.save(update_fields=["output_path"])
        return attempt

    def test_lists_saved_files_with_run_context_and_no_host_paths(self):
        older = self.execution("failed")
        ContainerExecution.objects.filter(pk=older.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        latest = self.execution()
        output = Path(latest.output_path)
        (output / "checkpoints").mkdir(parents=True)
        (output / "checkpoints" / "model.pt").write_bytes(b"model")
        (output / "metrics.json").write_text("{}")
        (output / "shortcut.pt").symlink_to(self.root / "outside.pt")
        response = self.client.get("/api/projects/outputs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        run = response.json()[0]
        self.assertEqual(run["execution_id"], str(latest.pk))
        self.assertEqual(run["project_name"], "Example")
        self.assertEqual(run["entrypoint"], "src/train.py")
        self.assertEqual(run["run_number"], 2)
        self.assertEqual(
            [file["path"] for file in run["files"]],
            ["checkpoints/model.pt", "metrics.json"],
        )
        self.assertEqual(run["files"][0]["size_bytes"], 5)
        self.assertFalse(run["truncated"])
        self.assertNotIn("output_path", run)

    def test_downloads_nested_file_and_rejects_traversal_and_symlinks(self):
        attempt = self.execution()
        output = Path(attempt.output_path)
        (output / "checkpoints").mkdir(parents=True)
        (output / "checkpoints" / "model.pt").write_bytes(b"model bytes")
        outside = self.root / "outside.pt"
        outside.write_bytes(b"private")
        (output / "shortcut.pt").symlink_to(outside)
        (output / "linked-folder").symlink_to(outside.parent, target_is_directory=True)
        url = f"/api/projects/executions/{attempt.pk}/output"
        response = self.client.get(url, {"path": "checkpoints/model.pt"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"model bytes")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(
            self.client.get(url, {"path": "../outside.pt"}).status_code, 400
        )
        self.assertEqual(self.client.get(url, {"path": "shortcut.pt"}).status_code, 404)
        self.assertEqual(
            self.client.get(url, {"path": "linked-folder/outside.pt"}).status_code, 404
        )
        self.assertEqual(self.client.get(url, {"path": "missing.pt"}).status_code, 404)

    def test_empty_output_list(self):
        self.assertEqual(self.client.get("/api/projects/outputs").json(), [])
