# training-server

Standalone homelab training service, starting with container execution.

## Architecture

Today: `train-submit → DockerExecutor → Docker daemon → GPU training container`.
`gpu-runner` exposes submission, inspection, logs and stop using the same executor.
The existing homelab `train-submit`/`gpu-run` scripts remain in their old repository;
these new entry points are a replacement scaffold, not command-compatible copies.
Install in this project's virtual environment to avoid replacing the existing tools.

Next: Django accepts a request into a Job model; a separate worker reads jobs and
calls this executor. Django owns durable state. This milestone has no scheduler,
GPU exclusivity, automatic retries, lifecycle database or background worker.
Only submit one job at a time. Docker owns the process after start, so disconnecting
SSH does not terminate training. The executor runs on the Docker host with a local
Docker context; bind mount paths refer to that host.

## Setup

Requires Python 3.11+, Docker daemon access, a working NVIDIA host driver and
NVIDIA Container Toolkit configured for Docker. GPU jobs are trusted local code.
See https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/sample-workload.html
for the prerequisite GPU container check. The sample image uses CUDA 12.4; verify
host driver compatibility. The image tag is a baseline, not a tested host result.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
docker build -t training-server/smoke:local examples/smoke-training
.venv/bin/train-submit --image training-server/smoke:local --gpu 0
```

The command prints a job name, container ID and directory. Training uses the host
user's UID/GID, one selected GPU, no network, and a writable `/output` bind mount.
Code and dependencies are baked into the image. There is no host Python environment
inside the job. Network-dependent dataset downloads must happen before this job.

```bash
.venv/bin/gpu-runner status CONTAINER_ID
.venv/bin/gpu-runner logs CONTAINER_ID
.venv/bin/gpu-runner stop CONTAINER_ID
```

Override the image command after `--`, for example:

```bash
.venv/bin/train-submit --image training-server/smoke:local --gpu 0 -- python /app/train.py --epochs 10 --pause 0
```

Outputs live under `~/.local/state/training-server/jobs/JOB_ID/output/`:
`metrics.jsonl` and `checkpoint.pt`. Submission metadata and container ID are stored
alongside output. Status comes directly from Docker, including the resolved image ID.
A successful submission means Docker accepted startup, not that training succeeded.
Check `state.Status == "exited"` and `state.ExitCode == 0`, then verify artifacts.

Completed containers are retained for inspection and Docker logs. Export logs with
`gpu-runner logs CONTAINER_ID > train.log 2>&1` before explicit cleanup using
`docker rm CONTAINER_ID` on a stopped job. Host artifacts survive container removal.
Do not automatically resubmit after a timeout: inspect the printed job name first;
a Docker client failure may leave a created or running container. This scaffold does
not recover submission records automatically. Docker operations use finite timeouts.

## Validation

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Tests validate argument handling and container ownership without a Docker daemon.
On-host acceptance: build the image, submit the smoke training, wait for exit zero,
inspect logs and verify both output files. Also submit with `-- python -c 'raise
SystemExit(7)'` and confirm exit code 7 is retained. Test `stop` with a long job.
GPU execution and host prerequisites must be validated separately from unit tests.
