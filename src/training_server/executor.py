"""Small Docker adapter shared by the CLI and a future Django worker."""

import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath

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

    def capture_project_files(self, container: str, output: Path):
        """Save changed regular files under /app from a stopped container."""
        self.inspect(container)
        changes = self._run("diff", container)
        for line in changes.splitlines():
            if not line.startswith(("A /app/", "C /app/")):
                continue
            path = line[2:]
            relative = PurePosixPath(path).relative_to("/app")
            if (
                not relative.parts
                or ".." in relative.parts
                or any("\\" in part or "\0" in part for part in relative.parts)
            ):
                continue
            self._copy_regular_file(container, path, output, relative)

    @staticmethod
    def _copy_regular_file(
        container: str, source: str, output: Path, relative: PurePosixPath
    ):
        # Docker can copy a stopped container. Read the archive header first so
        # changed directories do not recursively copy the whole project.
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                ["docker", "cp", f"{container}:{source}", "-"],
                stdout=subprocess.PIPE,
                stderr=errors,
            )
            directory_fd = None
            temporary = None
            try:
                with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                    member = next(iter(archive), None)
                    if member is None or not member.isfile():
                        process.terminate()
                        process.wait(timeout=30)
                        return
                    directory_fd = os.open(
                        output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                    )
                    for part in ("project-files", *relative.parts[:-1]):
                        try:
                            os.mkdir(part, dir_fd=directory_fd)
                        except FileExistsError:
                            pass
                        next_fd = os.open(
                            part,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory_fd,
                        )
                        os.close(directory_fd)
                        directory_fd = next_fd
                    temporary = ".capture-" + uuid.uuid4().hex
                    target_fd = os.open(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory_fd,
                    )
                    with os.fdopen(target_fd, "wb") as target:
                        source_file = archive.extractfile(member)
                        if source_file is None:
                            raise ValueError(f"Could not read project file {source}")
                        shutil.copyfileobj(source_file, target)
                # Consume the remaining tar stream before checking Docker's exit.
                for _ in iter(lambda: process.stdout.read(65536), b""):
                    pass
                if process.wait(timeout=60):
                    errors.seek(0)
                    raise subprocess.CalledProcessError(
                        process.returncode,
                        process.args,
                        stderr=errors.read().decode(errors="replace"),
                    )
                os.replace(
                    temporary,
                    relative.parts[-1],
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                temporary = None
            except tarfile.TarError as exc:
                raise ValueError(f"Could not read project file {source}") from exc
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if temporary is not None:
                    os.unlink(temporary, dir_fd=directory_fd)
                if directory_fd is not None:
                    os.close(directory_fd)
