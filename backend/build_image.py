"""Bounded image-build subprocess with a durable result for worker recovery."""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run_build(directory, timeout, stop=None):
    from django.utils import timezone

    from backend.services.environment_validation import export_snapshot

    stop = stop or threading.Event()
    request = json.loads((directory / "request.json").read_text())
    context = directory / "context"
    context.mkdir()
    result = {"success": False, "error": "", "log": ""}
    process = None
    log_path = directory / "build.log"
    try:
        if stop.is_set() or (directory / "cancel").exists():
            raise ValueError("Image build cancelled.")
        export_snapshot(SimpleNamespace(**request["project"]), context)
        (context / "Dockerfile").write_text(request["dockerfile"])
        if stop.is_set() or (directory / "cancel").exists():
            raise ValueError("Image build cancelled.")
        with log_path.open("wb") as output:
            process = subprocess.Popen(
                [
                    "docker",
                    "build",
                    "--tag",
                    request["tag"],
                    "--iidfile",
                    str(directory / "image-id"),
                    "--file",
                    str(context / "Dockerfile"),
                    str(context),
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if stop.is_set() or (directory / "cancel").exists():
                    raise ValueError("Image build cancelled.")
                if time.monotonic() >= deadline:
                    raise ValueError(f"Image build timed out after {timeout} seconds.")
                stop.wait(0.2)
            if process.returncode:
                raise ValueError(
                    f"Image build failed with exit code {process.returncode}."
                )
        image = (directory / "image-id").read_text().strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("Docker did not return a valid image ID.")
        result.update(success=True, image=image)
    except Exception as exc:
        # This process reports failures to its parent through a durable result,
        # including export failures; it never updates or starts a training job.
        result["error"] = str(exc)
    finally:
        if process is not None:
            stop_process(process)
        if log_path.exists():
            with log_path.open("rb") as output:
                output.seek(max(0, log_path.stat().st_size - 8000))
                result["log"] = output.read().decode(errors="replace")
        result["finished_at"] = timezone.now().isoformat()
        # Keep bounded diagnostics and the result, not another copy of the code.
        if log_path.exists():
            log_path.write_text(result["log"])
        write_json(directory / "result.json", result)
        shutil.rmtree(context)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--timeout", type=int, required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    args = parser.parse_args()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.config.settings")
    import django

    django.setup()
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    try:
        run_build(args.directory, args.timeout, stop)
    finally:
        os.close(args.lock_fd)


if __name__ == "__main__":
    main()
