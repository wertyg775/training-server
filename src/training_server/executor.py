"""Small Docker adapter shared by the CLI and a future Django worker."""

import json
import os
import re
import subprocess
from pathlib import Path

LABEL = "training-server.managed"


class DockerExecutor:
    def _run(self, *args):
        return subprocess.run(
            ["docker", *args], check=True, capture_output=True, text=True, timeout=60
        ).stdout.strip()

    def create(
        self, job_id: str, image: str, output: Path, command: list[str], gpu: str = "0"
    ) -> str:
        if not re.fullmatch(r"job-[a-f0-9]{32}", job_id):
            raise ValueError("Invalid job ID")
        if not image or image.startswith("-"):
            raise ValueError("Supply an image name")
        if not re.fullmatch(r"(?:[0-9]+|GPU-[a-fA-F0-9-]+)", gpu):
            raise ValueError("Select one GPU index or UUID")
        output = output.resolve(strict=True)
        if not output.is_dir() or "," in str(output):
            raise ValueError("Output must be a directory with no comma in its path")
        return self._run(
            "create",
            "--name",
            job_id,
            "--label",
            LABEL + "=true",
            "--gpus",
            "device=" + gpu,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--network",
            "none",
            "--shm-size",
            "1g",
            "--env",
            "PYTHONUNBUFFERED=1",
            "--env",
            "GPU_JOB_OUTPUT_DIR=/output",
            "--mount",
            f"type=bind,src={output},dst=/output",
            image,
            *command,
        )

    def inspect(self, container: str) -> dict:
        if not re.fullmatch(r"(?:[a-f0-9]{12,64}|job-[a-f0-9]{32})", container):
            raise ValueError("Supply a container ID or generated job name")
        info = json.loads(self._run("inspect", container))[0]
        if info["Config"].get("Labels", {}).get(LABEL) != "true":
            raise ValueError("Container is not managed by training-server")
        return info

    def start(self, container: str):
        self.inspect(container)
        self._run("start", container)

    def logs(self, container: str):
        self.inspect(container)
        # Inherit both streams: Docker sends container stderr to CLI stderr.
        subprocess.run(["docker", "logs", container], check=True, timeout=60)

    def stop(self, container: str):
        self.inspect(container)
        self._run("stop", "--timeout", "30", container)
