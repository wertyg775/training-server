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
