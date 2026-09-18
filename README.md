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

## Django backend scaffold

`backend/` is the installed Django app:

- `models.py`: immutable projects, training jobs and container executions.
- `config/`: settings, URL configuration, ASGI and WSGI entry points.
- `api/__init__.py`: API assembly using Django Ninja.
- `api/routers/`: health and project import endpoints.
- `services/`: project import and local storage handling.
- `management/commands/`: Django's standard custom command package. The future
  `runworker.py` defines `Command(BaseCommand)` with a `handle()` method.
- `migrations/`: model migrations.

```bash
uv sync
uv run python manage.py check
uv run python manage.py migrate
uv run python manage.py test backend
uv run python manage.py runserver 127.0.0.1:8000
```

Visit `/api/health` for liveness and `/api/docs` for OpenAPI documentation.
This scaffold has no training submission endpoint, authentication or polling
worker yet. Existing CLI commands still invoke Docker locally. Use the backend
on localhost while those pieces are developed. Deployment must supply
`DJANGO_SECRET_KEY` and appropriate `DJANGO_ALLOWED_HOSTS`; settings also accept
`TRAINING_DATABASE` and `DJANGO_DEBUG` (default off).

## Frontend development

With Django running on `127.0.0.1:8000`, start the React dashboard:

```bash
cd frontend
npm install
npm run dev
```

Vite proxies `/api` requests to Django. `npm run build` produces `frontend/dist`;
production hosting must route `/api` to Django. Run API client tests with `npm test`.
The Projects page lists only ready projects, with search and selection. The upload
API helper is available in `src/api.js`; Upload Files remains intentionally unwired.
Revision displays `-` when absent; Actions displays `-` until actions are available.

## Project import API

Requires Git on the backend host. Imports run synchronously and return a project
ID, status and resolved commit. The project remains a single immutable snapshot.

```bash
curl -X POST http://127.0.0.1:8000/api/projects/upload \
  -F 'name=My project' -F 'file=@source.zip'
curl -X POST http://127.0.0.1:8000/api/projects/git \
  -H 'Content-Type: application/json' \
  -d '{"name":"My project","repository_url":"https://github.com/owner/repo.git"}'
```

ZIP uploads preserve relative paths (zip the folder contents to avoid a wrapper
directory). Supplied `.git` metadata is discarded and the uploaded files are
committed into a new repository, including files matched by `.gitignore`.
Unsafe archive paths and symlinks are rejected. Default limits are 100 MiB for
uploads, 500 MiB extracted and 10,000 archive entries, configurable in settings.
Git imports clone the default branch of an HTTPS repository without credentials;
private repository authentication, submodules and Git LFS fetching are not supported.
Git commands have a configurable 120-second timeout.

Storage defaults to `project/<project-id>/`. Set the `PROJECT_STORAGE_ROOT`
environment variable to change the local root. Settings read the process
environment; automatic `.env` loading and blob storage are future work.
Failed imports return HTTP 400 with a failed project record and remove partial
repository files. Invalid request fields return HTTP 422; successful imports return
HTTP 201. Container launch is a separate future endpoint.

`GET /api/projects` lists every project; `GET /api/projects/ready` lists only ready
projects. Both include ZIP and Git imports and sort newest first. These provide
separate views for future management and dashboard pages, but do not add roles
or access control.

`GET /api/projects/<project-id>/files` lists the committed root directory.
Pass `?path=src` to list a subdirectory. The response contains `path` and an
`entries` array with `name`, project-relative `path`, and `type` (directory,
file, symlink or submodule). Directories sort first, then entries sort by name.
Only ready projects can be browsed: other statuses return HTTP 409, missing
projects return 404, and invalid directory paths return 400. Symlinks and
submodules are listed but cannot be expanded.

Migration `0002` brings the existing models into the database. It refuses to
convert pre-existing image-based jobs automatically, because they have no Git
snapshot to reference; those require an explicit migration or archival first.
