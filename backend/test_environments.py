import copy
import tomllib
from unittest.mock import patch

from django.test import SimpleTestCase

from backend.services.dockerfiles import prepare_dockerfile, validate_uv_project
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
