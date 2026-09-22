# Progress

- Added per-job file/ZIP dataset uploads, persistent training controls with job status and expiry display, and bounded storage outside project snapshots.
- Added Docker-backed job start/retry/cancel commands and completion reconciliation with dataset cleanup after 24 hours.
- Added retention and upload regression tests, a maintenance timer, and deployment instructions; protected cleanup against concurrent dataset claims on SQLite and PostgreSQL.
- Fixed duplicate filename, epoch, and submit controls when switching files by keeping one persistent training form.
- Added training Dockerfile generation from uv project snapshots with download API, frontend link, packaging dependency, seed venv exclusion, and uv-training example.
- Added Docker-based training environment validation with management command, report field/API, and tests.
- Documented API architecture under `docs/ARCHITECTURE.md`.
- Added `AGENTS.md` agent guidance and this progress file.
