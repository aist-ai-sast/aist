from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

from celery.schedules import crontab

PRODUCT_BASE_DIR = Path(__file__).resolve().parent.parent
VENDOR_BASE_DIR = PRODUCT_BASE_DIR / "vendor" / "defectdojo"

if str(VENDOR_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_BASE_DIR))

from dojo.settings.settings import *  # noqa: F403,E402

# Core Django wiring for the product.
ROOT_URLCONF = "aist_site.urls"
WSGI_APPLICATION = "aist_site.wsgi.application"
ASGI_APPLICATION = "aist_site.asgi.application"

# Register AIST app.
extra_apps = [app for app in ("django_github_app", "aist.apps.AistConfig") if app not in INSTALLED_APPS]  # noqa: F405
if extra_apps:
    INSTALLED_APPS = [*extra_apps, *INSTALLED_APPS]  # noqa: F405

# AIST paths and feature flags.
AIST_PIPELINE_CODE_PATH = env(  # noqa: F405
    "AIST_PIPELINE_CODE_PATH",
    default=str(PRODUCT_BASE_DIR / "sast-combinator" / "sast-pipeline"),
)

AIST_PROJECTS_BUILD_DIR = env("AIST_PROJECTS_BUILD_DIR", default="/tmp/aist/projects")  # noqa: F405,S108

PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", default="https://157.90.113.55:8443/")  # noqa: F405
AIST_AI_TRIAGE_WEBHOOK_URL = env(  # noqa: F405
    "AIST_AI_TRIAGE_WEBHOOK_URL",
    default="https://flaming.app.n8n.cloud/webhook/triage-sast",
)
AIST_AI_TRIAGE_SECRET = ""

REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"] += ("rest_framework.permissions.IsAuthenticated",)  # noqa: F405

GITHUB_APP = {
    "WEBHOOK_SECRET": env("WEBHOOK_SECRET", default=""),  # noqa: F405
    "APP_ID": env("GITHUB_APP_ID", default=""),  # noqa: F405
    "CLIENT_ID": env("GITHUB_CLIENT_ID", default=""),  # noqa: F405
    "NAME": env("GITHUB_APP_NAME", default=""),  # noqa: F405
    "WEBHOOK_TYPE": env("WEBHOOK_TYPE", default=""),  # noqa: F405
    "PRIVATE_KEY": env("PRIVATE_KEY", default=""),  # noqa: F405
}

# TODO: must be overridden in production.
FIELD_ENCRYPTION_KEY = env(  # noqa: F405
    "FIELD_ENCRYPTION_KEY",
    default="8fXhDgOkQXCi2TjuPcomS0swNpj6ynTVuT3H2QrwZlk=",
)

LOGIN_EXEMPT_URLS += (r"^aist/pipelines/[^/]+/callback/?$", r"^aist/github_hook/")  # noqa: F405

CELERY_TASK_IGNORE_RESULT = False

# Add AIST Celery schedules.
CELERY_BEAT_SCHEDULE.update(  # noqa: F405
    {
        "reconcile-deduplication": {
            "task": "aist.reconcile_deduplication",
            "schedule": crontab(minute="*/2"),
            "kwargs": {"batch_size": 200, "max_runtime_s": 50},
        },
        "aist-schedule-launches": {
            "task": "aist.tasks.launch_schedule.process_launch_schedules",
            "schedule": timedelta(minutes=1),
        },
        "aist-dispatch-queued": {
            "task": "aist.tasks.pipeline_dispatcher.dispatch_queued_pipelines",
            "schedule": timedelta(minutes=1),
        },
    },
)

# Logging extensions for GitHub App.
LOGGING["loggers"].setdefault(  # noqa: F405
    "github_app",
    {"handlers": [f"{LOGGING_HANDLER}"], "level": str(LOG_LEVEL), "propagate": True},  # noqa: F405
)
LOGGING["loggers"].setdefault(  # noqa: F405
    "django_github_app",
    {"handlers": [f"{LOGGING_HANDLER}"], "level": str(LOG_LEVEL), "propagate": True},  # noqa: F405
)

# Ensure cloud banner is disabled by default in the product.
CREATE_CLOUD_BANNER = env.bool("DD_CREATE_CLOUD_BANNER", False)  # noqa: F405
