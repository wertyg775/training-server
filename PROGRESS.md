# Progress

- Captured new and changed project files from completed training containers into each run's Outputs folder.
- Added an Outputs page that groups saved files by run, browses nested folders, and downloads files without exposing storage paths.
- Added an Executions dashboard with project and job context, search, selection, and navigation, backed by an execution list API.
- Added a Honcho Procfile and uv dev dependency to start the backend, frontend, and training worker together.
- Removed the redundant maintenance command and verified the worker reconciles executions before claiming jobs and cleans up expired datasets.
- Fixed container dispatch for lowercase Docker missing-container errors, with executor and worker regression coverage.
- Verified real image build and container creation; GPU startup remains blocked by missing host NVIDIA container runtime/CDI configuration.
- Added database queue claiming with per-job and per-GPU reservations and canonical GPU UUID resolution.
- Added isolated snapshot image builds with durable results, cancellation, bounded restart recovery, and failure diagnostics.
- Added a persistent worker and service configuration for automatic training, monitoring, retries, and dataset cleanup.
- Added worker tests covering reservation conflicts, inherited build locks, restart recovery, cancellation, and database transaction boundaries.
- Added per-job file/ZIP dataset uploads, status and expiry display, and cleanup 24 hours after completion, failure, or cancellation.
- Kept one persistent filename, epoch, and submit control when switching files.
- Added Dockerfile generation, environment validation, examples, and architecture/deployment documentation.
- Added TrainingJobsPage mirroring the Projects dashboard layout (no Upload Files button) with search, selection, empty/loading/error states, and sidebar navigation between pages; backed by a new `GET /api/projects/training-jobs` list endpoint returning job metadata with project names.

- Added queued startup checks with GPU reservations, bounded observation, captured output, and explicit submission after success.
- Added separate dataset uploads and project-relative file/ZIP mounts; made epoch arguments optional.
- Documented startup checks and dataset mappings; added lifecycle, cleanup recovery, submission gating, and mount regression coverage.
- Applied the schema migration; verified 70 backend tests, 7 executor tests, frontend API tests, and the production frontend build.
- Restyled the project browser as a connected directory tree with folder/file icons, folders first, and expandable branches that retain nested expansion state.
- Moved the Dockerfile download into a collapsed Advanced section in the training form.
- Moved dataset uploads to a compact modal with a blurred backdrop, opened from the directory panel; removed inline instructions and defaulted previews to a Python file.
