"""Local development settings for the training backend."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
PROJECT_STORAGE_ROOT = Path(
    os.environ.get("PROJECT_STORAGE_ROOT", str(BASE_DIR / "project"))
)
PROJECT_UPLOAD_MAX_BYTES = 100 * 1024 * 1024
PROJECT_EXTRACT_MAX_BYTES = 500 * 1024 * 1024
PROJECT_UPLOAD_MAX_FILES = 10000
PROJECT_GIT_TIMEOUT = 120
LOCAL_DB_PATH = BASE_DIR / "data" / "db.sqlite3"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "development-only-training-server")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = os.environ.get(
    "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]"
).split(",")
INSTALLED_APPS = ["backend.apps.BackendConfig"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]
ROOT_URLCONF = "backend.config.urls"
WSGI_APPLICATION = "backend.config.wsgi.application"
ASGI_APPLICATION = "backend.config.asgi.application"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("TRAINING_DATABASE", str(LOCAL_DB_PATH)),
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
