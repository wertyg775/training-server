"""Host-local operation locks; never hold SQL transactions across Docker calls."""

import fcntl
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def attempt_directory(execution_id):
    return Path(settings.TRAINING_WORK_ROOT).resolve() / str(execution_id)


def attempt_lock(execution_id):
    return file_lock(attempt_directory(execution_id) / "operation.lock")
