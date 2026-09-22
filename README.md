# training-server

Homelab training service with a Django backend and React dashboard.

## Setup

With `uv`, Git, and Node.js/npm installed, run from the repository root:

```bash
uv sync
uv run pre-commit install
cp .env.example .env
```

This installs Python dependencies and the pre-commit and commit-message hooks.
Skip the copy if you already have a `.env` file.

## Database and backend

Set `DATABASE_URL` in `.env`. SQLite works locally out of the box; once PostgreSQL
is installed and its user/database exist, use your connection details:

```dotenv
DATABASE_URL=postgresql://user:password@localhost:5432/training
```

Start the backend:

```bash
mkdir -p data
uv run python manage.py migrate
uv run python manage.py seed_projects
uv run python manage.py runserver 127.0.0.1:8000
```

Seeding adds the bundled example projects and can be rerun safely.
API documentation: http://127.0.0.1:8000/api/docs.
After switching databases, rerun migrations and seeding; existing data is not transferred.

## Frontend

In another terminal:

```bash
cd frontend
npm ci
npm run dev
```

Open the URL printed by Vite. API requests are proxied to the backend on port 8000.

## Checks

```bash
uv run pre-commit run --all-files
uv run python manage.py test backend
uv run python -m unittest discover -s tests -v
npm --prefix frontend test
npm --prefix frontend run build
```

GPU training additionally requires Docker, an NVIDIA driver, and NVIDIA Container Toolkit.

## Training Dockerfiles

Submitting a training request saves a Dockerfile recipe with the job and provides
a download link. Files are read from the imported Git snapshot; the project itself
is never modified. Existing root `Dockerfile` files are preserved verbatim.

When no Dockerfile exists, generation requires a uv project with root
`pyproject.toml` (including a `[project]` table) and `uv.lock`. The recipe installs
the project with `uv sync --locked --no-dev --no-editable`.

Prepare the training project with `uv init` if needed, declare dependencies with
`uv add <package>`, and run `uv lock` before importing it. Existing requirements
can be migrated with `uv add -r requirements.txt`. Projects without dependencies
still need a pyproject and lockfile. Missing files produce an actionable error
on submission. uv resolves declared dependencies; it does not infer packages
from Python imports. Before generating, the server validates static project
metadata, dependency syntax, lockfile structure and the locked root project's
name/version. Full dependency/lock freshness checks happen during the build.

The image's CPython version comes from `.python-version` (3.10–3.14, optionally
including a patch version), or a compatible `requires-python` version, preferring
3.12. Patch-specific constraints may require an explicit patch pin. uv uses the
image's interpreter and cannot silently download a different Python. The build
runs `uv sync --locked` and `uv pip check`. The selected script and epochs become
the generated image's default command; the script must accept `--epochs`.

Automatic generation supports static, managed, single-project uv environments.
Dynamic metadata, uv workspaces, and separate `uv.toml` configuration require a
custom Dockerfile (or moving compatible uv settings into `pyproject.toml`).

Download the Dockerfile into the matching project root to use it as a Docker
build context. Keep local `.venv`, `.git`, credentials and datasets out of the
context using your project's `.dockerignore`. Custom OS packages, CUDA toolkits,
nonstandard dependency formats and private package credentials require a
user-supplied Dockerfile or build configuration. Dependency resolution happens
during image building; generation does not verify package availability or CUDA
compatibility. A stale or incompatible lockfile causes the image build to fail
instead of silently changing dependency versions.

Submission prepares the recipe only. Automatic image building and training
execution from the dashboard are not implemented yet. Existing jobs created
before this feature do not have a downloadable recipe.

## Validating a generated environment

Check Docker from the terminal that will run the backend commands:

```bash
id
ls -l /var/run/docker.sock
docker info
docker run --rm hello-world
```

Submit a new training request and use its displayed job ID:

```bash
uv run python manage.py validate_training_environment <job-id>
```

The command exports the committed snapshot into a temporary build context,
excludes Git metadata, local virtual environments and `.env` files, rejects
symlinks/submodules, builds the saved Dockerfile, and checks Python plus script
syntax inside the image. For custom Dockerfiles, validation expects Python on
PATH and the selected script at `/app/<entrypoint>`. It records the image ID,
completed steps, output and failures on the job. The report is also available at
`GET /api/projects/<project-id>/training-jobs/<job-id>`.

Add checks for actual runtime imports, a small PyTorch GPU operation, or execution
of the image's default command:

```bash
uv run python manage.py validate_training_environment <job-id> --import-module numpy
uv run python manage.py validate_training_environment <job-id> --import-module torch --check-cuda
uv run python manage.py validate_training_environment <job-id> --run-entrypoint
```

The CUDA check requires PyTorch in the project and uses the job's requested GPU.
`--run-entrypoint` executes the actual image command, including the saved epochs
for generated recipes; use a small smoke job. Runtime checks have no network,
run as your UID with a read-only root filesystem and temporary writable `/tmp`
and `/output`, and stop after at most 120 seconds. Outputs are discarded. The
build timeout defaults to 900 seconds and can be set with `--timeout`. Temporary
containers and the validation image tag are cleaned up; Docker build cache may
remain. Import checks execute those modules; successful checks establish only
the tested imports, Python environment, script syntax and optional CUDA/default
command behavior, not the correctness of an entire training workload.

The bundled `examples/uv-training` has a uv lockfile, a small NumPy regression
script, and no Dockerfile. Seed it using `uv run python manage.py seed_projects`,
select its `train.py`, and submit with one epoch to exercise generation.

Run the opt-in integration tests to build/run that example and verify a stale
lockfile fails its image build (requires Docker and image/package downloads):

```bash
TRAINING_DOCKER_TESTS=1 uv run python manage.py test backend.test_environments
```

### Training datasets and retention

Select a Python file in a project, choose an optional dataset beside the training
controls, set epochs, and submit. Upload a single file (CSV, Parquet, etc.) or a
ZIP containing nested dataset folders. ZIPs are extracted with their relative
paths preserved; symlinks, special files, unsafe paths, encrypted archives, and
oversized uploads are rejected. Datasets are stored separately from the project's
Git snapshot and are never added to its generated Docker build context.

Each dataset belongs to exactly one job. Existing jobs without a dataset continue
to work. The upload API is `POST /api/projects/<project-id>/datasets` with multipart
`file`; pass its returned `id` as `dataset_id` when submitting a training job.
The job detail API includes `status`, `finished_at`, and dataset metadata including
`expires_at` and `deleted_at`. The training form refreshes these values while open.

Apply the schema before starting the updated application:

```bash
uv run python manage.py migrate
```

Start the database-polling worker to build and execute queued jobs automatically:

```bash
uv run python manage.py training_worker
# Manage more than one GPU, with one active job per GPU:
uv run python manage.py training_worker --gpus 0 1
```

The default GPU is `0`. Set `TRAINING_GPUS=0,1` in `.env` or pass `--gpus` to
configure worker capacity. Job submissions may specify `requested_gpu` as a GPU
index or full UUID; the frontend currently submits to GPU 0. The worker resolves
indexes with `nvidia-smi` and stores UUIDs so two aliases cannot reserve the same
GPU. It claims the oldest queued job for each free configured GPU. A GPU remains
reserved during building, startup, and training. Database constraints prevent
multiple active executions for a job or GPU, including manual starts.

The worker builds the saved Dockerfile against the exact committed snapshot;
local uncommitted files and datasets are excluded from the build context. Builds
run in separate processes outside database transactions, allowing monitoring and
cancellation to continue while other images build. The resulting immutable image
ID, build timestamps, and the last 8,000 characters of build output are saved on
the execution. Build errors appear on the job and in the frontend. Build contexts
are removed after completion; image tags and Docker build cache remain available
for inspection. `TRAINING_BUILD_TIMEOUT` defaults to 1,800 seconds per build.

Manual troubleshooting and lifecycle commands remain available:

```bash
uv run python manage.py cancel_training_job <job-id>
uv run python manage.py retry_training_job <job-id>
uv run python manage.py run_training_job <job-id> --image training:example
uv run python manage.py maintain_training
```

Cancellation stops training or signals an active image builder. Retries put a
terminal job back into the queue, provided its dataset has not expired. The
manual image command shares the worker's GPU reservations. `maintain_training`
is a one-off execution/retention check; the persistent worker owns image builds.

The image's default command must run the selected script with the saved arguments.
The runner mounts the dataset read-only at `/dataset` and sets
`GPU_JOB_DATASET_DIR=/dataset`. Scripts should read that environment variable to
locate their inputs. A single uploaded file is available under its original
filename; a ZIP exposes its extracted files. Outputs go to `/output`, available
through `GPU_JOB_OUTPUT_DIR`, and are retained separately for each execution.
The older `train-submit` / `gpu-run submit` commands remain standalone and do not
update Django job records. Environment validation is a smoke check, not training
completion, and does not start dataset expiry.

Jobs move from `queued` to `building` to `running`, then `finished` (exit code zero), `failed`,
or `cancelled`. Completion reconciliation uses Docker's finish timestamp, so
expiry is 24 hours after actual completion, even when monitoring was delayed.
Failures and cancellations use the same TTL. Run `retry_training_job` to
retry a terminal job before its dataset expires: this clears the old deadline
until the new attempt ends. Expired data requires a new upload and job. Uploads
never assigned to a job expire 24 hours after upload. Dataset database records,
job history, and training outputs survive payload deletion.

The worker polls every two seconds by default and sweeps expired datasets every
minute, even with an empty queue. Cleanup happens on the first successful sweep
at or after the deadline. Docker connection failures preserve active reservations
and datasets until container state can be confirmed.

Only one worker runs per `TRAINING_WORK_ROOT` (default `data/worker`). This version
supports one Linux host and Docker daemon, with a shared local work directory,
database, and storage configuration for the worker and manual commands. An
exclusive process lock prevents duplicate workers. Operation locks serialize
container actions without keeping database transactions open. SQLite uses
immediate transactions for claims; PostgreSQL uses row locks. GPU reservation
constraints apply to Django-managed jobs; standalone CLI jobs are independent.

On restart, the worker resumes monitoring existing containers by saved ID or
deterministic name. A live image builder retains an inherited lock and may finish
even after its worker exits. Its durable result is consumed on the next start.
An interrupted builder without a result is restarted from the saved snapshot
with a fresh build token; after three interrupted launches, the job fails for
manual retry. Cancellation takes precedence over a build result. Neither a
worker restart nor a Docker outage automatically starts a second training attempt.

For continuous operation, install the user-systemd service (adjust the checkout
and uv paths if needed). If the old maintenance timer was installed, disable it
with `systemctl --user disable --now training-maintenance.timer` first.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/training-worker.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now training-worker.service
journalctl --user -u training-worker.service -f
```

The user manager must remain active for unattended operation. Run the worker as
the backend's user with the same database and storage, Docker access, and
`nvidia-smi` on PATH. `--poll-interval` controls polling; `--once` performs one
sweep for diagnostics and may launch a builder. SIGTERM/SIGINT stop new claims;
saved attempts are recovered next time. The systemd unit also stops child build
processes on service shutdown. Migrations and service activation are deployment
steps and are not performed by frontend submission.

Storage defaults to `data/datasets` and `data/outputs`; configure
`DATASET_STORAGE_ROOT` and `TRAINING_OUTPUT_ROOT` to override these locations.
`DATASET_UPLOAD_MAX_BYTES` defaults to 5 GiB and `DATASET_EXTRACT_MAX_BYTES` to
20 GiB, with at most 100,000 ZIP entries. Configure your HTTP server's request
size and timeout limits to accommodate the intended uploads.
