import copy
import io
import os
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase

from backend.services.dockerfiles import prepare_dockerfile, validate_uv_project
from backend.services.environment_validation import (
    export_snapshot,
    validate_environment,
)
from backend.services.projects import import_project
from backend.services.training import submit_training
from backend.tests import UV_PROJECT_FILES


class ProjectMetadataTests(SimpleTestCase):
    def setUp(self):
        self.metadata = tomllib.loads(UV_PROJECT_FILES["pyproject.toml"])
        self.lock = tomllib.loads(UV_PROJECT_FILES["uv.lock"])

    def test_selects_python_and_checks_explicit_pin(self):
        self.metadata["project"]["requires-python"] = ">=3.13,<3.14"
        self.assertEqual(validate_uv_project(self.metadata, self.lock), "3.13")
        self.assertEqual(
            validate_uv_project(self.metadata, self.lock, "3.13.7"), "3.13.7"
        )
        for pin in ["3.12", "3.15", "3.13\nRUN bad", "pypy3.13"]:
            with self.subTest(pin=pin), self.assertRaises(ValueError):
                validate_uv_project(self.metadata, self.lock, pin)

    def test_rejects_invalid_static_metadata(self):
        for replacement in [
            {"name": ""},
            {"name": "bad name"},
            {"version": ""},
            {"version": 2},
            {"requires-python": "potato"},
            {"requires-python": [">=3"]},
            {"dependencies": "numpy"},
            {"dependencies": ["not a valid requirement !"]},
            {"dynamic": ["dependencies"]},
        ]:
            metadata = copy.deepcopy(self.metadata)
            metadata["project"].update(replacement)
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                validate_uv_project(metadata, self.lock)
        with self.assertRaises(ValueError):
            validate_uv_project({"project": {}}, self.lock)

    def test_rejects_missing_or_mismatched_lock_metadata(self):
        for lock in [
            {},
            {"version": True, "package": []},
            {"version": 1, "package": []},
        ]:
            with self.subTest(lock=lock), self.assertRaises(ValueError):
                validate_uv_project(self.metadata, lock)
        self.lock["package"][0]["version"] = "2.0.0"
        with self.assertRaisesMessage(ValueError, "does not match"):
            validate_uv_project(self.metadata, self.lock)

    def test_rejects_unmanaged_projects_and_workspaces(self):
        for config in [{"managed": False}, {"workspace": {"members": ["packages/*"]}}]:
            self.metadata["tool"] = {"uv": config}
            with self.subTest(config=config), self.assertRaises(ValueError):
                validate_uv_project(self.metadata, self.lock)

    def test_generated_command_preserves_special_filename_as_one_argument(self):
        files = {**UV_PROJECT_FILES, ".python-version": "3.12\n"}
        with (
            patch(
                "backend.services.dockerfiles.list_project_files",
                return_value={
                    "entries": [{"name": name, "type": "file"} for name in files]
                },
            ),
            patch(
                "backend.services.dockerfiles.read_project_file",
                side_effect=lambda project, name: {"content": files[name]},
            ),
        ):
            recipe, _ = prepare_dockerfile(
                "project", 'src/train "special".py', ["--epochs", "2"]
            )
        import json

        command = json.loads(recipe.split("CMD ", 1)[1])
        self.assertEqual(
            command, ["python", '/app/src/train "special".py', "--epochs", "2"]
        )
        self.assertIn("FROM python:3.12-slim-bookworm", recipe)
        self.assertIn("uv pip check", recipe)


class EnvironmentValidationTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        settings = self.settings(PROJECT_STORAGE_ROOT=self.root / "projects")
        settings.enable()
        self.addCleanup(settings.disable)

    def create_job(self, files=None):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            for name, content in (
                files or {**UV_PROJECT_FILES, "train.py": "print('ok')"}
            ).items():
                zipped.writestr(name, content)
        project = import_project(
            name="Validation",
            upload=SimpleUploadedFile("project.zip", archive.getvalue()),
        )
        return submit_training(project.pk, "train.py", 1)

    def fake_docker(self, arguments, **kwargs):
        if arguments[:2] == ["docker", "build"]:
            iidfile = Path(arguments[arguments.index("--iidfile") + 1])
            iidfile.write_text("sha256:" + "a" * 64)
        return subprocess.CompletedProcess(arguments, 0, stdout="passed", stderr="")

    def test_export_reads_commit_and_excludes_local_environment_files(self):
        job = self.create_job(
            {
                **UV_PROJECT_FILES,
                "train.py": "print('original')",
                ".env": "secret",
                ".venv/secret": "secret",
            }
        )
        Path(job.project.storage_path, "train.py").write_text("changed")
        context = self.root / "context"
        context.mkdir()
        export_snapshot(job.project, context)
        self.assertEqual((context / "train.py").read_text(), "print('original')")
        self.assertFalse((context / ".env").exists())
        self.assertFalse((context / ".venv").exists())

    def test_export_rejects_symlinks_and_traversal(self):
        job = self.create_job()
        for name, kind in [("escape", tarfile.SYMTYPE), ("../escape", tarfile.REGTYPE)]:
            with self.subTest(name=name):

                def archive(*arguments, **kwargs):
                    if "archive" in arguments:
                        output = next(
                            arg.removeprefix("--output=")
                            for arg in arguments
                            if arg.startswith("--output=")
                        )
                        with tarfile.open(output, "w") as target:
                            member = tarfile.TarInfo(name)
                            member.type = kind
                            member.linkname = "/tmp/outside"
                            target.addfile(member)
                    return ""

                with (
                    patch(
                        "backend.services.environment_validation._git",
                        side_effect=archive,
                    ),
                    self.assertRaises(ValueError),
                ):
                    export_snapshot(job.project, self.root / "context")
        self.assertFalse((self.root / "escape").exists())

    def test_export_preserves_executable_bit_without_special_permissions(self):
        job = self.create_job()

        def archive(*arguments, **kwargs):
            if "archive" in arguments:
                output = next(
                    arg.removeprefix("--output=")
                    for arg in arguments
                    if arg.startswith("--output=")
                )
                with tarfile.open(output, "w") as target:
                    member = tarfile.TarInfo("script.sh")
                    member.mode = 0o4755
                    target.addfile(member)
            return ""

        context = self.root / "context"
        context.mkdir()
        with patch("backend.services.environment_validation._git", side_effect=archive):
            export_snapshot(job.project, context)
        self.assertEqual((context / "script.sh").stat().st_mode & 0o7777, 0o755)

    def test_success_records_image_runtime_checks_and_cleans_only_owned_tag(self):
        job = self.create_job()
        with (
            patch("subprocess.run", side_effect=self.fake_docker) as docker,
            patch("backend.services.environment_validation.export_snapshot"),
        ):
            report = validate_environment(job, imports=["numpy"], check_cuda=True)
        self.assertEqual(report["status"], "passed")
        job.refresh_from_db()
        self.assertEqual(job.environment_validation["image_id"], "sha256:" + "a" * 64)
        commands = [call.args[0] for call in docker.call_args_list]
        run = next(command for command in commands if command[:2] == ["docker", "run"])
        self.assertIn("--read-only", run)
        self.assertIn("--gpus", run)
        self.assertIn("torch.ones", run[-1])
        self.assertIn("compile(p.read_bytes()", run[-1])
        removal = commands[-1]
        self.assertEqual(removal[:3], ["docker", "image", "rm"])
        self.assertTrue(removal[-1].startswith("training-validation:"))
        response = self.client.get(
            f"/api/projects/{job.project_id}/training-jobs/{job.pk}"
        )
        self.assertEqual(response.json()["environment_validation"]["status"], "passed")

    def test_docker_access_failure_is_recorded_without_build(self):
        job = self.create_job()

        def denied(arguments, **kwargs):
            return subprocess.CompletedProcess(
                arguments, 1, stdout="", stderr="permission denied"
            )

        with patch("backend.services.environment_validation.export_snapshot") as export:
            report = validate_environment(job, runner=denied)
        self.assertEqual(report["status"], "failed")
        self.assertIn("permission denied", report["error"])
        export.assert_not_called()

    def test_build_failure_never_runs_container_or_claims_pass(self):
        job = self.create_job()
        calls = []

        def failure(arguments, **kwargs):
            calls.append(arguments)
            if arguments[:2] == ["docker", "build"]:
                return subprocess.CompletedProcess(
                    arguments, 1, stdout="", stderr="lockfile needs updating"
                )
            return self.fake_docker(arguments, **kwargs)

        with patch("backend.services.environment_validation.export_snapshot"):
            report = validate_environment(job, runner=failure)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["steps"], ["Docker access"])
        self.assertFalse(any(command[:2] == ["docker", "run"] for command in calls))

    def test_runtime_timeout_cleans_container_and_records_failure(self):
        job = self.create_job()
        calls = []

        def timeout(arguments, **kwargs):
            calls.append(arguments)
            if arguments[:2] == ["docker", "run"]:
                raise subprocess.TimeoutExpired(arguments, 1)
            return self.fake_docker(arguments, **kwargs)

        with patch("backend.services.environment_validation.export_snapshot"):
            report = validate_environment(job, runner=timeout, timeout=1)
        self.assertEqual(report["status"], "failed")
        self.assertIn("timed out", report["error"])
        self.assertTrue(
            any(command[:3] == ["docker", "rm", "--force"] for command in calls)
        )

    def test_invalid_import_request_never_contacts_docker(self):
        job = self.create_job()
        with patch("subprocess.run") as docker, self.assertRaises(ValueError):
            validate_environment(job, imports=["numpy; exec('bad')"])
        docker.assert_not_called()

    @skipUnless(
        os.environ.get("TRAINING_DOCKER_TESTS") == "1",
        "Set TRAINING_DOCKER_TESTS=1 to run real Docker builds.",
    )
    def test_real_generated_image_builds_and_runs(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "uv-training"
        job = self.create_job(
            {
                path.name: path.read_text()
                for path in example.iterdir()
                if path.is_file()
            }
        )
        report = validate_environment(job, imports=["numpy"], run_entrypoint=True)
        self.assertEqual(report["status"], "passed", report)
        self.assertIn('"loss":', report["entrypoint_output"])

    @skipUnless(
        os.environ.get("TRAINING_DOCKER_TESTS") == "1",
        "Set TRAINING_DOCKER_TESTS=1 to run real Docker builds.",
    )
    def test_real_build_rejects_stale_lock(self):
        files = {**UV_PROJECT_FILES, "train.py": "pass"}
        files["pyproject.toml"] = files["pyproject.toml"].replace(
            "dependencies=[]", 'dependencies=["numpy"]'
        )
        job = self.create_job(files)
        report = validate_environment(job)
        self.assertEqual(report["status"], "failed", report)
        self.assertIn("Image build failed", report["error"])
        self.assertIn("needs to be updated", report["error"])
