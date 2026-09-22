# Progress

- Added database queue claiming with per-job and per-GPU reservations and canonical GPU UUID resolution.
- Added isolated snapshot image builds with durable results, cancellation, bounded restart recovery, and failure diagnostics.
- Added a persistent worker and service configuration for automatic training, monitoring, retries, and dataset cleanup.
- Added worker tests covering reservation conflicts, inherited build locks, restart recovery, cancellation, and database transaction boundaries.
- Added per-job file/ZIP dataset uploads, status and expiry display, and cleanup 24 hours after completion, failure, or cancellation.
- Kept one persistent filename, epoch, and submit control when switching files.
- Added Dockerfile generation, environment validation, examples, and architecture/deployment documentation.
