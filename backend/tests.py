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

from backend.models import ContainerExecution, Project, TrainingJob
from backend.services.dockerfiles import prepare_dockerfile
from backend.services.projects import list_project_files, list_ready_projects
from backend.services.training import list_training_jobs

UV_PROJECT_FILES = {
    "pyproject.toml": '[project]\nname="training"\nversion="0.1.0"\nrequires-python=">=3.12"\ndependencies=[]\n',
    "uv.lock": 'version = 1\nrevision = 3\nrequires-python = ">=3.12"\n\n[[package]]\nname = "training"\nversion = "0.1.0"\nsource = { virtual = "." }\n',
}


class DockerfilePreparationTests(SimpleTestCase):
    def test_does_not_follow_a_dockerfile_symlink(self):
        with (
            patch(
                "backend.services.dockerfiles.list_project_files",
                return_value={"entries": [{"name": "Dockerfile", "type": "symlink"}]},
            ),
            patch("backend.services.dockerfiles.read_project_file") as read,
            self.assertRaisesMessage(ValueError, "regular file"),
        ):
            prepare_dockerfile("project", "train.py", [])
        read.assert_not_called()


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
    def test_training_jobs_list_returns_metadata_newest_first(self):
        project = Project.objects.create(
            name="Demo", source_type=Project.SourceType.UPLOAD
        )
        other = Project.objects.create(name="Other", source_type=Project.SourceType.GIT)
        oldest = TrainingJob.objects.create(
            project=project, entrypoint="old.py", requested_gpu="0"
        )
        newest = TrainingJob.objects.create(
            project=other, entrypoint="src/train.py", requested_gpu="1"
        )
        TrainingJob.objects.filter(pk=oldest.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        response = self.client.get("/api/projects/training-jobs")
        self.assertEqual(response.status_code, 200)
        jobs = response.json()
        self.assertEqual([job["id"] for job in jobs], [str(newest.pk), str(oldest.pk)])
        self.assertEqual(jobs[0]["project_name"], "Other")
        self.assertEqual(jobs[0]["entrypoint"], "src/train.py")
        self.assertEqual(jobs[0]["status"], "queued")
        self.assertEqual(jobs[0]["requested_gpu"], "1")
        self.assertIsNone(jobs[0]["dataset"])
        self.assertEqual(jobs[1]["project_name"], "Demo")
        self.assertEqual(list(list_training_jobs()), [newest, oldest])

    def test_training_jobs_list_empty(self):
        response = self.client.get("/api/projects/training-jobs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_submit_training_request(self):
        self.upload({"src/train.py": "pass", **UV_PROJECT_FILES})
        project = Project.objects.get()
        # Requests refer to committed files even when the worktree changes.
        (Path(project.storage_path) / "src/train.py").unlink()
        response = self.client.post(
            f"/api/projects/{project.pk}/training-jobs",
            {"entrypoint": "src/train.py", "epochs": 12},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        job = TrainingJob.objects.get()
        self.assertEqual(response.json()["id"], str(job.pk))
        self.assertEqual(job.project_id, project.pk)
        self.assertEqual(job.entrypoint, "src/train.py")
        self.assertEqual(job.arguments, ["--epochs", "12"])
        self.assertEqual(job.requested_gpu, "0")
        self.assertFalse(ContainerExecution.objects.exists())
        self.assertEqual(job.dockerfile_source, "generated")
        self.assertIn(
            'CMD ["python", "/app/src/train.py", "--epochs", "12"]', job.dockerfile
        )
        self.assertIn("uv sync --locked --no-dev --no-editable", job.dockerfile)
        self.assertNotIn(
            "Dockerfile",
            [entry["name"] for entry in list_project_files(project.pk)["entries"]],
        )
        download = self.client.get(
            f"/api/projects/{project.pk}/training-jobs/{job.pk}/dockerfile"
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content.decode(), job.dockerfile)
        self.assertEqual(
            download["Content-Disposition"], 'attachment; filename="Dockerfile"'
        )

    def submit(self, project):
        return self.client.post(
            f"/api/projects/{project.pk}/training-jobs",
            {"entrypoint": "train.py", "epochs": 2},
            content_type="application/json",
        )

    def test_preserves_committed_dockerfile_without_dependency_detection(self):
        original = "# custom\r\nFROM example/custom:1\r\n"
        self.upload(
            {"train.py": "pass", "Dockerfile": original, "pyproject.toml": "invalid"}
        )
        project = Project.objects.get()
        (Path(project.storage_path) / "Dockerfile").write_text("changed")
        response = self.submit(project)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["dockerfile_source"], "project")
        self.assertEqual(TrainingJob.objects.get().dockerfile, original)

    def test_generation_reads_uv_manifests_from_snapshot(self):
        self.upload(
            {
                "train.py": "pass",
                "requirements.txt": "ignored-package",
                **UV_PROJECT_FILES,
            }
        )
        project = Project.objects.get()
        (Path(project.storage_path) / "pyproject.toml").unlink()
        (Path(project.storage_path) / "uv.lock").write_text("invalid")
        response = self.submit(project)
        self.assertEqual(response.status_code, 201, response.content)
        recipe = TrainingJob.objects.get().dockerfile
        self.assertIn("uv sync --locked", recipe)
        self.assertNotIn("uv pip install", recipe)

    def test_generation_errors_do_not_save_jobs(self):
        for files, message in [
            ({}, "requires a uv project"),
            ({"requirements.txt": "numpy"}, "requires a uv project"),
            ({"pyproject.toml": UV_PROJECT_FILES["pyproject.toml"]}, "uv lock"),
            ({**UV_PROJECT_FILES, "pyproject.toml": "invalid"}, "not valid TOML"),
            ({**UV_PROJECT_FILES, "uv.lock": "invalid"}, "uv.lock is not valid TOML"),
            ({**UV_PROJECT_FILES, "pyproject.toml": "[tool.ruff]"}, "[project] table"),
            ({"pyproject.toml/child": "", "uv.lock": ""}, "regular file"),
            ({"Dockerfile/child": ""}, "regular file"),
        ]:
            with self.subTest(files=files):
                response = self.upload({"train.py": "pass", **files})
                project = Project.objects.get(pk=response.json()["id"])
                response = self.submit(project)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn(message, response.json()["detail"])
        self.assertFalse(TrainingJob.objects.exists())

    def test_dockerfile_download_scopes_job_to_project_and_handles_legacy_jobs(self):
        self.upload({"train.py": "pass", **UV_PROJECT_FILES})
        project = Project.objects.get()
        response = self.submit(project)
        job_id = response.json()["id"]
        other = Project.objects.create(
            name="Other", source_type=Project.SourceType.UPLOAD
        )
        self.assertEqual(
            self.client.get(
                f"/api/projects/{other.pk}/training-jobs/{job_id}/dockerfile"
            ).status_code,
            404,
        )
        TrainingJob.objects.filter(pk=job_id).update(
            dockerfile="", dockerfile_source=""
        )
        self.assertEqual(
            self.client.get(
                f"/api/projects/{project.pk}/training-jobs/{job_id}/dockerfile"
            ).status_code,
            404,
        )

    def test_training_request_validation(self):
        self.upload(
            {"train.py": "pass", "Dockerfile": "FROM scratch", "dir.py/child": ""}
        )
        project = Project.objects.get()
        url = f"/api/projects/{project.pk}/training-jobs"
        for epochs in [0, -1, 1.5, True, "3", None, 2147483648]:
            with self.subTest(epochs=epochs):
                response = self.client.post(
                    url,
                    {"entrypoint": "train.py", "epochs": epochs},
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 422)
        for entrypoint in [
            "Dockerfile",
            "../train.py",
            "/train.py",
            "missing.py",
            "dir.py",
            "dir\\train.py",
            "bad\0.py",
        ]:
            with self.subTest(entrypoint=entrypoint):
                response = self.client.post(
                    url,
                    {"entrypoint": entrypoint, "epochs": 1},
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)
        for status in [Project.Status.IMPORTING, Project.Status.FAILED]:
            Project.objects.filter(pk=project.pk).update(status=status)
            response = self.client.post(
                url,
                {"entrypoint": "train.py", "epochs": 1},
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 409)
        project.delete()
        response = self.client.post(
            url,
            {"entrypoint": "train.py", "epochs": 1},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(TrainingJob.objects.exists())

    def test_training_request_rejects_symlink(self):
        with patch(
            "backend.services.training.list_project_files",
            return_value={
                "entries": [{"name": "train.py", "type": "symlink"}],
            },
        ):
            response = self.client.post(
                "/api/projects/00000000-0000-0000-0000-000000000000/training-jobs",
                {"entrypoint": "train.py", "epochs": 1},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(TrainingJob.objects.exists())

    def test_file_preview_reads_snapshot_and_preserves_whitespace(self):
        content = "  # café\r\nprint('<script>')\r\n\n"
        self.upload({"src/my file.py": content, "empty.txt": ""})
        project = Project.objects.get()
        (Path(project.storage_path) / "src/my file.py").write_text("changed")
        url = f"/api/projects/{project.pk}/file"
        response = self.client.get(url, {"path": "src/my file.py"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"path": "src/my file.py", "content": content}
        )
        self.assertEqual(
            self.client.get(url, {"path": "empty.txt"}).json()["content"], ""
        )
        for path in [
            "../secret",
            "/etc/passwd",
            "src/../../secret",
            "src\\file",
            "\0",
            ".git/config",
            "missing",
            "src",
            "",
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(url, {"path": path}).status_code, 400)
        Project.objects.filter(pk=project.pk).update(status=Project.Status.FAILED)
        self.assertEqual(self.client.get(url, {"path": "empty.txt"}).status_code, 409)
        project.delete()
        self.assertEqual(self.client.get(url, {"path": "empty.txt"}).status_code, 404)

    def test_file_preview_rejects_binary_large_files_and_symlinks(self):
        self.upload(
            {
                "binary": b"hello\0world",
                "invalid": b"\xff",
                "large": "x" * (1024 * 1024 + 1),
                "normal": "text",
            }
        )
        project = Project.objects.get()
        url = f"/api/projects/{project.pk}/file"
        for path in ["binary", "invalid", "large"]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(url, {"path": path}).status_code, 400)
        with (
            patch(
                "backend.services.projects.list_project_files",
                return_value={
                    "path": "",
                    "entries": [{"name": "link", "path": "link", "type": "symlink"}],
                },
            ),
            patch("backend.services.projects._git") as git,
        ):
            self.assertEqual(self.client.get(url, {"path": "link"}).status_code, 400)
            git.assert_not_called()

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
