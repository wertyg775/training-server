import io
import subprocess
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from backend.models import Project


class HealthTests(SimpleTestCase):
    def test_health_endpoint(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class ProjectListTests(TestCase):
    def test_empty_list(self):
        response = self.client.get("/api/projects")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_lists_all_projects_by_creation_time_descending(self):
        newest = Project.objects.create(
            name="Newest", source_type=Project.SourceType.UPLOAD
        )
        oldest = Project.objects.create(
            name="Oldest", source_type=Project.SourceType.GIT
        )
        Project.objects.filter(pk=oldest.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        response = self.client.get("/api/projects")
        self.assertEqual(response.status_code, 200)
        projects = response.json()
        self.assertEqual(
            [project["id"] for project in projects], [str(newest.pk), str(oldest.pk)]
        )
        self.assertIn("created_at", projects[0])
        self.assertNotIn("storage_path", projects[0])


class ProjectImportTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        override = self.settings(PROJECT_STORAGE_ROOT=self.root)
        override.enable()
        self.addCleanup(override.disable)

    def upload(self, files):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return self.client.post(
            "/api/projects/upload",
            {
                "name": "Example",
                "file": SimpleUploadedFile("project.zip", buffer.getvalue()),
            },
        )

    def test_upload_creates_committed_snapshot(self):
        response = self.upload(
            {"train.py": "print('train')", ".git/config": "untrusted"}
        )
        self.assertEqual(response.status_code, 201, response.content)
        project = Project.objects.get()
        self.assertEqual(project.status, Project.Status.READY)
        self.assertEqual(Path(project.storage_path).parent, self.root)
        contents = subprocess.check_output(
            ["git", "show", f"{project.resolved_commit}:train.py"],
            cwd=project.storage_path,
            text=True,
        )
        self.assertEqual(contents, "print('train')")
        self.assertNotIn("storage_path", response.json())

    def test_traversal_fails_and_cleans_storage(self):
        response = self.upload({"../escaped.py": "bad"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Project.objects.get().status, Project.Status.FAILED)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_zip_fails(self):
        response = self.client.post(
            "/api/projects/upload",
            {"name": "Example", "file": SimpleUploadedFile("bad.zip", b"not zip")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_extraction_limit(self):
        with self.settings(PROJECT_EXTRACT_MAX_BYTES=2):
            response = self.upload({"train.py": "large"})
        self.assertEqual(response.status_code, 400)

    def test_upload_limit(self):
        with self.settings(PROJECT_UPLOAD_MAX_BYTES=2):
            response = self.upload({"train.py": "large"})
        self.assertEqual(response.status_code, 400)

    def test_git_import(self):
        with patch(
            "backend.services.projects._git", side_effect=["", "a" * 40, ""]
        ) as git:
            response = self.client.post(
                "/api/projects/git",
                {"name": "Remote", "repository_url": "https://example.com/repo.git"},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Project.objects.get().resolved_commit, "a" * 40)
        self.assertEqual(git.call_args_list[0].args[1], "clone")

    def test_git_failure_retains_failed_record(self):
        with patch(
            "backend.services.projects._git", side_effect=ValueError("Git failed")
        ):
            response = self.client.post(
                "/api/projects/git",
                {"name": "Remote", "repository_url": "https://example.com/repo.git"},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Project.objects.get().status, Project.Status.FAILED)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_reject_local_repository_url(self):
        response = self.client.post(
            "/api/projects/git",
            {"name": "Remote", "repository_url": "file:///etc"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(Project.objects.exists())
