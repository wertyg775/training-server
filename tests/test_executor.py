import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from training_server.executor import LABEL, DockerExecutor


class ExecutorTests(unittest.TestCase):
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

        with patch.object(
            DockerExecutor,
            "_run",
            side_effect=subprocess.CalledProcessError(
                1, "docker", stderr="Error: No such object: abc"
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
