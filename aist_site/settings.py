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


if "aist_site.middleware.AistResponseMaskingMiddleware" not in MIDDLEWARE:  # noqa: F405
    middleware = list(MIDDLEWARE)  # noqa: F405
    middleware.insert(0, "aist_site.middleware.AistResponseMaskingMiddleware")
    MIDDLEWARE = middleware

# Guard DefectDojo UI from non-superusers while keeping API access intact.
if "aist_site.middleware.AistAdminGuardMiddleware" not in MIDDLEWARE:
    middleware = list(MIDDLEWARE)
    try:
        auth_index = middleware.index("django.contrib.auth.middleware.AuthenticationMiddleware")
    except ValueError:
        auth_index = len(middleware) - 1
    middleware.insert(auth_index + 1, "aist_site.middleware.AistAdminGuardMiddleware")
    MIDDLEWARE = middleware

# Register AIST app.
extra_apps = [app for app in ("django_github_app", "aist.apps.AistConfig") if app not in INSTALLED_APPS]  # noqa: F405
if extra_apps:
    INSTALLED_APPS = [*extra_apps, *INSTALLED_APPS]  # noqa: F405

# AIST paths and feature flags.
AIST_PIPELINE_CODE_PATH = env(  # noqa: F405
    "AIST_PIPELINE_CODE_PATH",
    default=str(PRODUCT_BASE_DIR / "sast-combinator" / "sast-pipeline"),
)

# Ensure admin auth redirects stay within the protected prefix.
LOGIN_URL = "/aist-admin/login/"
LOGIN_REDIRECT_URL = "/aist-admin/"

AIST_PROJECTS_BUILD_DIR = env("AIST_PROJECTS_BUILD_DIR", default="/tmp/aist/projects")  # noqa: F405,S108

PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", default="https://aist.itsec-europe.com/")  # noqa: F405
AIST_AI_TRIAGE_WEBHOOK_URL = env(  # noqa: F405
    "AIST_AI_TRIAGE_WEBHOOK_URL",
    default="https://flaming.app.n8n.cloud/webhook/triage-sast",
)
AIST_AI_TRIAGE_SECRET = ""

REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"] += ("rest_framework.permissions.IsAuthenticated",)  # noqa: F405
AIST_AUTH_LOGIN_THROTTLE_RATE = env("DD_AIST_AUTH_LOGIN_THROTTLE_RATE", default="10/min")  # noqa: F405
REST_FRAMEWORK.setdefault("DEFAULT_THROTTLE_RATES", {})["aist_auth_login"] = AIST_AUTH_LOGIN_THROTTLE_RATE  # noqa: F405

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

LOGIN_EXEMPT_URLS += (  # noqa: F405
    r"^aist/pipelines/[^/]+/callback/?$",
    r"^aist/github_hook/",
    r"^(?!aist-admin/|api/|projects_version/|aist/).*$",
)

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
        "aist-reconcile-orphans-safety-net": {
            "task": "aist.tasks.reconciliation.reconcile_recent_orphans",
            "schedule": timedelta(minutes=10),
            "kwargs": {"hours": 24, "batch_size": 200, "dry_run": False},
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

# Allow concurrent sessions for the same user across devices/browsers.
SINGLE_USER_SESSION = env.bool("DD_SINGLE_USER_SESSION", False)  # noqa: F405

# Keep regular logout local to the current browser session.
LOGOUT_ALL_SESSIONS = env.bool("DD_LOGOUT_ALL_SESSIONS", False)  # noqa: F405

AIST_CANONICAL_DEDUPE_SCAN_TYPES = (
    "Snyk Code Scan",
    "SnykCode Scan (Snyk Code Scan)",
    "Semgrep JSON Report",
    "Horusec Scan",
    "Bearer CLI",
)
AIST_CANONICAL_HASH_FIELDS = ["vuln_id_from_tool", "file_path", "line", "cwe"]

DEDUPLICATION_ALGORITHM_PER_PARSER = dict(globals().get("DEDUPLICATION_ALGORITHM_PER_PARSER", {}))
for _scan_type in AIST_CANONICAL_DEDUPE_SCAN_TYPES:
    DEDUPLICATION_ALGORITHM_PER_PARSER[_scan_type] = "unique_id_from_tool_or_hash_code"

HASHCODE_FIELDS_PER_SCANNER = dict(globals().get("HASHCODE_FIELDS_PER_SCANNER", {}))
for _scan_type in AIST_CANONICAL_DEDUPE_SCAN_TYPES:
    HASHCODE_FIELDS_PER_SCANNER[_scan_type] = AIST_CANONICAL_HASH_FIELDS

HASHCODE_ALLOWS_NULL_CWE = dict(globals().get("HASHCODE_ALLOWS_NULL_CWE", {}))
HASHCODE_ALLOWS_NULL_CWE["Horusec Scan"] = True

AIST_CANONICAL_AUTO_DUPLICATE_THRESHOLD = env.int("DD_AIST_CANONICAL_AUTO_DUPLICATE_THRESHOLD", default=2)  # noqa: F405
AIST_CANONICAL_CANDIDATE_MIN_SCORE = env.int("DD_AIST_CANONICAL_CANDIDATE_MIN_SCORE", default=1)  # noqa: F405

FINDING_DEDUPE_METHOD = "aist.dedupe.custom.custom_dedupe_finding"
FINDING_DEDUPE_BATCH_METHOD = "aist.dedupe.custom.custom_dedupe_batch"
