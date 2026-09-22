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
