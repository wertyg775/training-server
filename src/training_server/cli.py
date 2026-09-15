"""Initial local submission interface; Django will later own job records."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid

from .executor import DockerExecutor


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)

    submit = commands.add_parser("submit")
    submit.add_argument("--image", required=True, help="Already built/pulled training image")
    submit.add_argument("--gpu", default="0")
    submit.add_argument(
        "--output-root",
        type=Path,
        default=Path.home() / ".local/state/training-server/jobs",
    )
    submit.add_argument("command", nargs=argparse.REMAINDER)

    for action in ("status", "logs", "stop"):
        commands.add_parser(action).add_argument("container")

    return parser


def submit_job(args, executor):
    job_id = "job-" + uuid.uuid4().hex
    directory = args.output_root.expanduser().resolve() / job_id
    output = directory / "output"
    output.mkdir(parents=True, mode=0o700)

    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]

    request = {
        "id": job_id,
        "image": args.image,
        "gpu": args.gpu,
        "command": command,
        "output": str(output),
    }
    (directory / "request.json").write_text(json.dumps(request, indent=2) + "\n")
    print(f"Job: {job_id}\nDirectory: {directory}", flush=True)

    container = executor.create(job_id, args.image, output, command, args.gpu)
    (directory / "container-id").write_text(container + "\n")
    print(f"Container: {container}", flush=True)

    executor.start(container)


def show_status(args, executor):
    info = executor.inspect(args.container)
    status = {
        "id": info["Id"],
        "image": info["Image"],
        "state": info["State"],
    }
    print(json.dumps(status, indent=2))


def main(argv=None):
    args = build_parser().parse_args(argv)
    executor = DockerExecutor()

    try:
        if args.action == "submit":
            submit_job(args, executor)
        elif args.action == "status":
            show_status(args, executor)
        elif args.action == "logs":
            executor.logs(args.container)
        else:
            executor.stop(args.container)

    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"training-server: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        return 1

    return 0


def submit_main():
    return main(["submit", *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
