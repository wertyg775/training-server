"""Small Docker adapter shared by the CLI and a future Django worker."""

import json
import os
import re
import subprocess
from pathlib import Path

LABEL = "training-server.managed"


class ContainerNotFound(ValueError):
    pass


class DockerExecutor:
    def gpu_devices(self):
        """Resolve GPU indexes to stable UUIDs so aliases share a reservation."""
        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
        devices = {}
        for line in output.splitlines():
            index, separator, identifier = line.partition(",")
            index, identifier = index.strip(), identifier.strip()
            if (
                not separator
                or not index.isdigit()
                or not re.fullmatch(r"GPU-[a-fA-F0-9-]+", identifier)
            ):
                raise ValueError("Could not read GPU identities from nvidia-smi.")
            devices[index] = identifier
        if not devices:
            raise ValueError("No NVIDIA GPUs are available.")
        return devices

    def _run(self, *args):
        return subprocess.run(
            ["docker", *args], check=True, capture_output=True, text=True, timeout=60
        ).stdout.strip()

    def create(
        self,
        job_id: str,
        image: str,
        output: Path,
        command: list[str],
        gpu: str = "0",
        dataset: Path | None = None,
        dataset_target: str = "",
        dataset_file: str = "",
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
        dataset_args = []
        if dataset is not None:
            dataset = dataset.resolve(strict=True)
            if not dataset.is_dir() or "," in str(dataset):
                raise ValueError(
                    "Dataset must be a directory with no comma in its path"
                )
            target = "/dataset"
            if dataset_target:
                from pathlib import PurePosixPath

                location = PurePosixPath(dataset_target)
                if (
                    location.is_absolute()
                    or ".." in location.parts
                    or not location.parts
                    or any(c in dataset_target for c in "\\\0,\n\r")
                ):
                    raise ValueError("Invalid dataset location")
                target = "/app/" + location.as_posix()
                if dataset_file:
                    if Path(dataset_file).name != dataset_file:
                        raise ValueError("Invalid dataset filename")
                    dataset = (dataset / dataset_file).resolve(strict=True)
            dataset_args = [
                "--mount",
                f"type=bind,src={dataset},dst={target},readonly",
                "--env",
                f"GPU_JOB_DATASET_DIR={target}",
            ]
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
            *dataset_args,
            image,
            *command,
        )

    def inspect(self, container: str) -> dict:
        if not re.fullmatch(r"(?:[a-f0-9]{12,64}|job-[a-f0-9]{32})", container):
            raise ValueError("Supply a container ID or generated job name")
        try:
            raw = self._run("inspect", container)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").lower()
            if "no such object:" in stderr or "no such container:" in stderr:
                raise ContainerNotFound("Training container no longer exists.") from exc
            raise
        info = json.loads(raw)[0]
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

    def captured_logs(self, container: str):
        self.inspect(container)
        result = subprocess.run(
            ["docker", "logs", "--tail", "100", container],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return (result.stdout + result.stderr)[-8000:]

    def remove(self, container: str):
        self.inspect(container)
        self._run("rm", "--force", container)
