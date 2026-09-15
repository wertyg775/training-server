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
