import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from training_server.executor import LABEL, DockerExecutor


class ExecutorTests(unittest.TestCase):
    def test_capture_changed_project_files_only(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root)
            executor = DockerExecutor()
            with (
                patch.object(executor, "inspect"),
                patch.object(
                    executor,
                    "_run",
                    return_value=(
                        "C /app\nA /app/checkpoints\nA /app/checkpoints/model.pt\n"
                        "C /app/metrics.json\nA /other/secret\nD /app/old.pt\n"
                    ),
                ),
                patch.object(executor, "_copy_regular_file") as copy,
            ):
                executor.capture_project_files("a" * 64, output)
            self.assertEqual(
                [call.args[1] for call in copy.call_args_list],
                [
                    "/app/checkpoints",
                    "/app/checkpoints/model.pt",
                    "/app/metrics.json",
                ],
            )
            self.assertEqual(
                copy.call_args_list[-1].args[2:],
                (output, PurePosixPath("metrics.json")),
            )

    def test_copy_regular_file_skips_directories_and_links(self):
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "project-files" / "model.pt"
            real_popen = subprocess.Popen

            def archive_bytes(member):
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode="w") as archive:
                    info = tarfile.TarInfo("model.pt")
                    info.type = member
                    info.size = 5 if member == tarfile.REGTYPE else 0
                    archive.addfile(info, io.BytesIO(b"model") if info.size else None)
                return buffer.getvalue()

            def launch(data):
                archive_path = Path(root) / "archive.tar"
                archive_path.write_bytes(data)
                with patch(
                    "training_server.executor.subprocess.Popen",
                    side_effect=lambda *args, **kwargs: real_popen(
                        ["cat", str(archive_path)],
                        stdout=kwargs["stdout"],
                        stderr=kwargs["stderr"],
                    ),
                ):
                    DockerExecutor._copy_regular_file(
                        "a" * 64,
                        "/app/model.pt",
                        Path(root),
                        PurePosixPath("model.pt"),
                    )

            launch(archive_bytes(tarfile.REGTYPE))
            self.assertEqual(destination.read_bytes(), b"model")
            destination.unlink()
            launch(archive_bytes(tarfile.DIRTYPE))
            self.assertFalse(destination.exists())
            launch(archive_bytes(tarfile.SYMTYPE))
            self.assertFalse(destination.exists())

    def test_capture_rejects_symlinked_output_folder(self):
        with tempfile.TemporaryDirectory() as root:
            outside = Path(root) / "outside"
            outside.mkdir()
            output = Path(root) / "output"
            output.mkdir()
            (output / "project-files").symlink_to(outside, target_is_directory=True)
            archive_path = Path(root) / "archive.tar"
            with tarfile.open(archive_path, "w") as archive:
                member = tarfile.TarInfo("model.pt")
                member.size = 5
                archive.addfile(member, io.BytesIO(b"model"))
            real_popen = subprocess.Popen
            with patch(
                "training_server.executor.subprocess.Popen",
                side_effect=lambda *args, **kwargs: real_popen(
                    ["cat", str(archive_path)],
                    stdout=kwargs["stdout"],
                    stderr=kwargs["stderr"],
                ),
            ):
                with self.assertRaises(OSError):
                    DockerExecutor._copy_regular_file(
                        "a" * 64,
                        "/app/model.pt",
                        output,
                        PurePosixPath("model.pt"),
                    )
            self.assertFalse((outside / "model.pt").exists())

    def test_create_preserves_arguments_and_mounts_output(self):
        with tempfile.TemporaryDirectory() as root, patch("subprocess.run") as run:
            run.return_value.stdout = "a" * 64
            DockerExecutor().create(
                "job-" + "b" * 32,
                "training:test",
                Path(root),
                ["python", "train.py", "space ; $(literal)"],
                "0",
            )
            argv = run.call_args.args[0]
            self.assertEqual(argv[-3:], ["python", "train.py", "space ; $(literal)"])
            self.assertIn("device=0", argv)
            self.assertIn(f"type=bind,src={root},dst=/output", argv)
            self.assertNotIn("--rm", argv)
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_stop_rejects_unmanaged_container(self):
        with patch.object(
            DockerExecutor,
            "_run",
            return_value=json.dumps([{"Config": {"Labels": {}}}]),
        ) as run:
            with self.assertRaises(ValueError):
                DockerExecutor().stop("a" * 64)
            self.assertEqual(run.call_count, 1)

    def test_start_checks_ownership_before_starting(self):
        with patch.object(
            DockerExecutor,
            "_run",
            side_effect=[json.dumps([{"Config": {"Labels": {LABEL: "true"}}}]), ""],
        ) as run:
            DockerExecutor().start("a" * 64)
            self.assertEqual(run.call_args.args, ("start", "a" * 64))

    def test_invalid_identifier_never_calls_docker(self):
        with patch("subprocess.run") as run:
            with self.assertRaises(ValueError):
                DockerExecutor().inspect("--help")
            run.assert_not_called()

    def test_dataset_is_mounted_readonly_and_exposed_to_script(self):
        with tempfile.TemporaryDirectory() as root, patch("subprocess.run") as run:
            run.return_value.stdout = "a" * 64
            DockerExecutor().create(
                "job-" + "b" * 32, "training:test", Path(root), [], dataset=Path(root)
            )
            argv = run.call_args.args[0]
            self.assertIn(f"type=bind,src={root},dst=/dataset,readonly", argv)
            self.assertIn("GPU_JOB_DATASET_DIR=/dataset", argv)

    def test_missing_container_is_distinct_from_docker_outage(self):
        import subprocess

        from training_server.executor import ContainerNotFound

        for stderr in (
            "Error: No such object: abc",
            "error: no such object: abc",
            "Error response from daemon: No such container: abc",
            "error response from daemon: no such container: abc",
        ):
            with (
                self.subTest(stderr=stderr),
                patch.object(
                    DockerExecutor,
                    "_run",
                    side_effect=subprocess.CalledProcessError(
                        1, "docker", stderr=stderr
                    ),
                ),
            ):
                with self.assertRaises(ContainerNotFound):
                    DockerExecutor().inspect("a" * 64)
        with patch.object(
            DockerExecutor,
            "_run",
            side_effect=subprocess.CalledProcessError(
                1, "docker", stderr="Cannot connect to Docker daemon"
            ),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                DockerExecutor().inspect("a" * 64)

    def test_gpu_discovery_returns_stable_uuid_mapping(self):
        with patch("subprocess.run") as run:
            run.return_value.stdout = "0, GPU-00000000-0000-0000-0000-000000000000\n1, GPU-11111111-1111-1111-1111-111111111111\n"
            self.assertEqual(
                DockerExecutor().gpu_devices()["1"],
                "GPU-11111111-1111-1111-1111-111111111111",
            )
            self.assertEqual(run.call_args.args[0][0], "nvidia-smi")
            run.return_value.stdout = "unexpected output"
            with self.assertRaises(ValueError):
                DockerExecutor().gpu_devices()
