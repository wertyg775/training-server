import io
import subprocess
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from backend.models import Project
from backend.services.projects import list_project_files, list_ready_projects


class SeedProjectsTests(TestCase):
    def test_seeds_browsable_snapshots_and_skips_repeat_imports(self):
        with tempfile.TemporaryDirectory() as storage:
            with self.settings(PROJECT_STORAGE_ROOT=storage):
                output = io.StringIO()
                call_command("seed_projects", stdout=output)
                project = Project.objects.get(name="Example: smoke-training")
                project.full_clean()
                self.assertEqual(project.status, Project.Status.READY)
                files = list_project_files(project.pk)["entries"]
                self.assertIn("train.py", [entry["name"] for entry in files])
                self.assertIn("Dockerfile", [entry["name"] for entry in files])
                original_ids = set(Project.objects.values_list("pk", flat=True))
                call_command("seed_projects", stdout=output)
                self.assertEqual(
                    set(Project.objects.values_list("pk", flat=True)), original_ids
                )
                self.assertIn("already seeded", output.getvalue())


class HealthTests(SimpleTestCase):
    def test_health_endpoint(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class ProjectListTests(TestCase):
    def test_ready_projects_excludes_other_statuses_and_orders_newest_first(self):
        oldest = Project.objects.create(
            name="Old",
            source_type=Project.SourceType.GIT,
            status=Project.Status.READY,
            repository_url="https://github.com/example/project.git",
            requested_revision="main",
        )
        newest = Project.objects.create(
            name="New",
            source_type=Project.SourceType.UPLOAD,
            status=Project.Status.READY,
        )
        Project.objects.filter(pk=oldest.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        for status in [Project.Status.IMPORTING, Project.Status.FAILED]:
            Project.objects.create(
                name=status, source_type=Project.SourceType.UPLOAD, status=status
            )
        self.assertEqual(list(list_ready_projects()), [newest, oldest])
        response = self.client.get("/api/projects/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[1]["repository_url"], oldest.repository_url)
        self.assertEqual(response.json()[1]["requested_revision"], "main")
        self.assertEqual(
            [item["id"] for item in response.json()], [str(newest.pk), str(oldest.pk)]
        )
        response = self.client.get("/api/projects")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 4)

    def test_empty_list(self):
        response = self.client.get("/api/projects")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        response = self.client.get("/api/projects/ready")
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
    def test_directory_api(self):
        self.upload({"src/train.py": "pass", "README.md": "Example"})
        project = Project.objects.get()
        url = f"/api/projects/{project.pk}/files"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "path": "",
                "entries": [
                    {"name": "src", "path": "src", "type": "directory"},
                    {"name": "README.md", "path": "README.md", "type": "file"},
                ],
            },
        )
        response = self.client.get(url, {"path": "src"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "path": "src",
                "entries": [
                    {"name": "train.py", "path": "src/train.py", "type": "file"},
                ],
            },
        )
        for path in ["../", "/etc", "missing", "README.md"]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(url, {"path": path}).status_code, 400)
        for status in [Project.Status.IMPORTING, Project.Status.FAILED]:
            Project.objects.filter(pk=project.pk).update(status=status)
            with self.subTest(status=status):
                self.assertEqual(self.client.get(url).status_code, 409)
        project.delete()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(
            self.client.get("/api/projects/invalid/files").status_code, 422
        )

    def test_directory_listing_uses_snapshot_and_preserves_names(self):
        self.upload({"z/train.py": "pass", "a.py": "pass", "z/tab\tfile.py": "pass"})
        project = Project.objects.get()
        (Path(project.storage_path) / "untracked.py").write_text("pass")
        (Path(project.storage_path) / "a.py").unlink()
        self.assertEqual(
            list_project_files(project.pk),
            {
                "path": "",
                "entries": [
                    {"name": "z", "path": "z", "type": "directory"},
                    {"name": "a.py", "path": "a.py", "type": "file"},
                ],
            },
        )
        self.assertEqual(
            list_project_files(project.pk, "z/"),
            {
                "path": "z",
                "entries": [
                    {"name": "tab\tfile.py", "path": "z/tab\tfile.py", "type": "file"},
                    {"name": "train.py", "path": "z/train.py", "type": "file"},
                ],
            },
        )
        for path in [
            "../",
            "/etc",
            "z/../../etc",
            "z\\file",
            "\0",
            "missing",
            "a.py",
            ".git",
        ]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                list_project_files(project.pk, path)

    def test_directory_listing_requires_ready_project(self):
        project = Project.objects.create(
            name="Pending", source_type=Project.SourceType.UPLOAD
        )
        with self.assertRaisesMessage(ValueError, "not ready"):
            list_project_files(project.pk)
        project.delete()
        with self.assertRaises(Project.DoesNotExist):
            list_project_files("00000000-0000-0000-0000-000000000000")

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
