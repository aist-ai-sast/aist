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


for _middleware in reversed(
    (
        "aist_site.middleware.AistTokenScopeMiddleware",
        "aist_site.middleware.AistFindingBulkLockMiddleware",
        "aist_site.middleware.AistNoStoreHtmlMiddleware",
        "aist_site.middleware.AistResponseMaskingMiddleware",
    ),
):
    if _middleware not in MIDDLEWARE:  # noqa: F405
        middleware = list(MIDDLEWARE)  # noqa: F405
        middleware.insert(0, _middleware)
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
extra_apps = [
    app for app in ("django_vite", "django_github_app", "aist.apps.AistConfig") if app not in INSTALLED_APPS  # noqa: F405
]
if extra_apps:
    INSTALLED_APPS = [*extra_apps, *INSTALLED_APPS]  # noqa: F405

# AIST paths and feature flags.
AIST_PIPELINE_CODE_PATH = env(  # noqa: F405
    "AIST_PIPELINE_CODE_PATH",
    default=str(PRODUCT_BASE_DIR / "sast-combinator" / "sast-pipeline"),
)

DJANGO_VITE = {
    "default": {
        "dev_mode": env.bool("AIST_DJANGO_VITE_DEV_MODE", default=False),  # noqa: F405
        "app_client_class": "aist.vite.AistDjangoViteAppClient",
        "manifest_path": Path(
            env("AIST_DJANGO_VITE_MANIFEST_PATH", default="/app/client-ui/dist/assets/manifest.json"),  # noqa: F405
        ),
        "dev_server_protocol": env("AIST_DJANGO_VITE_DEV_SERVER_PROTOCOL", default="http"),  # noqa: F405
        "dev_server_host": env("AIST_DJANGO_VITE_DEV_SERVER_HOST", default="localhost"),  # noqa: F405
        "dev_server_port": env.int("AIST_DJANGO_VITE_DEV_SERVER_PORT", default=5173),  # noqa: F405
        "static_url_prefix": "/",
    },
}

# Ensure admin auth redirects stay within the protected prefix.
LOGIN_URL = "/aist-admin/login/"
LOGIN_REDIRECT_URL = "/aist-admin/"

AIST_PROJECTS_BUILD_DIR = env("AIST_PROJECTS_BUILD_DIR", default="/tmp/aist/projects")  # noqa: F405,S108
# Per-pipeline directory where the local triage skill writes its completion
# marker file. Bridge ``_watch_result_file`` polls this path so a clean
# triage run wakes the bridge up immediately instead of waiting for
# ``AIST_LOCAL_TRIAGE_TIMEOUT`` (default 3h).
AIST_TRIAGE_OUTPUT_DIR = env("AIST_TRIAGE_OUTPUT_DIR", default="/tmp/aist/triage-output")  # noqa: F405,S108
AIST_VPN_SIDECAR_IMAGE = env("AIST_VPN_SIDECAR_IMAGE", default="aist-vpn-sidecar:latest")  # noqa: F405

PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", default="https://aist.itsec-europe.com/")  # noqa: F405
AIST_AI_TRIAGE_WEBHOOK_URL = env(  # noqa: F405
    "AIST_AI_TRIAGE_WEBHOOK_URL",
    default="https://flaming.app.n8n.cloud/webhook/triage-sast",
)
AIST_AI_TRIAGE_SECRET = ""

# Local AI triage via Claude CLI bridge
AIST_LOCAL_TRIAGE_BRIDGE_SOCKET = env(  # noqa: F405
    "AIST_LOCAL_TRIAGE_BRIDGE_SOCKET",
    default="/run/claude-bridge/bridge.sock",
)
AIST_LOCAL_TRIAGE_TIMEOUT = int(env("AIST_LOCAL_TRIAGE_TIMEOUT", default="1800"))  # noqa: F405
AIST_WORKING_DIR = env("AIST_WORKING_DIR", default="/app/aist")  # noqa: F405

# Directory where per-pipeline `<id>.log` (celeryworker) and
# `<id>.bridge.log` (claude-bridge) files live. Must point at the same
# shared volume both containers mount — see docker-compose.yml. Empty
# string falls back to MEDIA_ROOT/aist_logs.
AIST_LOG_DIR = env("AIST_LOG_DIR", default="")  # noqa: F405

# Limits for the claude-diff-security analyzer. Values larger than the diff
# being analyzed force the skill into the "truncated" code path which marks
# the pipeline FINISHED_WITH_WARNINGS instead of analyzing partial data.
CLAUDE_DIFF_MAX_FILES = int(env("CLAUDE_DIFF_MAX_FILES", default="200"))  # noqa: F405
CLAUDE_DIFF_MAX_BYTES = int(env("CLAUDE_DIFF_MAX_BYTES", default="1000000"))  # noqa: F405

# Limits for full-project agent analyzers (e.g. claude-full-security). Per-project
# overrides live in AISTProject.profile.agent_analyzers.full_security; these are
# the global defaults used when no override is set.
AGENT_FULL_MAX_FILES = int(env("AGENT_FULL_MAX_FILES", default="1500"))  # noqa: F405
AGENT_FULL_MAX_BYTES = int(env("AGENT_FULL_MAX_BYTES", default="8000000"))  # noqa: F405
AGENT_FULL_MAX_FILE_BYTES = int(env("AGENT_FULL_MAX_FILE_BYTES", default="200000"))  # noqa: F405
AGENT_FULL_MAX_FINDINGS = int(env("AGENT_FULL_MAX_FINDINGS", default="50"))  # noqa: F405

REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"] += ("rest_framework.permissions.IsAuthenticated",)  # noqa: F405
# AIST scoped personal access tokens. Added LAST so it only runs after the stock
# Session/Basic/Token authenticators decline — the internal superuser service
# token (stock DRF Token) is never shadowed. Read/write scope is enforced by
# AistTokenScopeMiddleware (not a DRF permission, which AIST views override).
REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"] += ("aist.authentication.ScopedTokenAuthentication",)  # noqa: F405
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
        "aist-sync-work-item-providers": {
            "task": "aist.tasks.work_items.sync_all_work_item_providers",
            "schedule": timedelta(minutes=15),
        },
        "aist-cleanup-orphaned-vpn-containers": {
            "task": "aist.tasks.work_items.cleanup_orphaned_vpn_containers",
            "schedule": timedelta(hours=1),
            "kwargs": {"max_age_minutes": 240},
        },
        "aist-reap-warm-egress": {
            "task": "aist.tasks.egress.reap_egress",
            "schedule": timedelta(minutes=5),
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

# The SPA reads the CSRF cookie via JS to send X-CSRFToken on every mutating
# request.  DefectDojo defaults this to True (server-rendered UI doesn't need
# JS-readable cookies), but that breaks session-based SPAs: after login(),
# Django rotates the CSRF token, the new cookie value is unreadable by JS, so
# every subsequent POST (including logout) fails with 403.
CSRF_COOKIE_HTTPONLY = False

# Explicitly control SMTP TLS/SSL from env. In this fork DD_EMAIL_URL parsing does
# not reliably map TLS flag into EMAIL_USE_TLS, so keep a deterministic override.
EMAIL_USE_TLS = env.bool("DD_EMAIL_USE_TLS", default=bool(globals().get("EMAIL_USE_TLS", False)))  # noqa: F405
EMAIL_USE_SSL = env.bool("DD_EMAIL_USE_SSL", default=bool(globals().get("EMAIL_USE_SSL", False)))  # noqa: F405

AIST_CANONICAL_DEDUPE_SCAN_TYPES = (
    "Snyk Code Scan",
    "SnykCode Scan (Snyk Code Scan)",
    "Semgrep JSON Report",
    "Horusec Scan",
    "Bearer CLI",
    # Claude agent-bridge analyzers — imported via "Generic Findings Import"
    # parser but renamed to these test_type names by aist/internal_upload.py
    # so canonical dedupe handles them as a first-class scan type.
    "Claude Diff Security",
    "Claude Full Security",
    "Claude Intake Review",
    "Claude Intake Diff",
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
# Claude analyzers may emit findings without a CWE (e.g. when the model declines
# to assign one). Allow the vendor fallback to dedup such findings via
# (vuln_id_from_tool, file_path, line) without filtering them out for null CWE.
HASHCODE_ALLOWS_NULL_CWE["Claude Diff Security"] = True
HASHCODE_ALLOWS_NULL_CWE["Claude Full Security"] = True
HASHCODE_ALLOWS_NULL_CWE["Claude Intake Review"] = True
HASHCODE_ALLOWS_NULL_CWE["Claude Intake Diff"] = True

AIST_CANONICAL_AUTO_DUPLICATE_THRESHOLD = env.int("DD_AIST_CANONICAL_AUTO_DUPLICATE_THRESHOLD", default=2)  # noqa: F405
AIST_CANONICAL_CANDIDATE_MIN_SCORE = env.int("DD_AIST_CANONICAL_CANDIDATE_MIN_SCORE", default=1)  # noqa: F405

FINDING_DEDUPE_METHOD = "aist.dedupe.custom.custom_dedupe_finding"
FINDING_DEDUPE_BATCH_METHOD = "aist.dedupe.custom.custom_dedupe_batch"
