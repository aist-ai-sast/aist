"""
Warm per-VPN egress gateway — the single owner of the interactive file-fetch
tunnel used by the UI (code view + findings-list snippet previews).

Why this module exists
----------------------
Blob fetches run in the **web** process, which has no Docker socket and cannot
tolerate the ~30 s OpenVPN cold start on the request path.  Instead we keep a
long-lived sidecar per VPN integration, started ahead of time from a Celery
worker (see ``aist/tasks/egress.py``) and reused across requests.  The web
process only derives the deterministic proxy URL and connects to it.

Responsibility boundary (kept deliberately narrow):
- ``aist/utils/vpn.py`` owns the low-level "how to run/stop the sidecar image"
  mechanics (credentials, image build, network, readiness).  This module never
  rebuilds a ``docker run`` line — it calls :func:`vpn.start_named_sidecar`.
- This module owns the warm-egress *policy*: the name scheme, which VPN applies
  to a project version, ensure/reuse, the pool (list, idle reaper, LRU cap).
- ``aist/api/files.py`` owns HTTP semantics; it calls
  :func:`proxy_url_for_project_version` and treats a connection error as "cold".

Invariants: one container per ``vpn_integration_id`` (a VPN integration belongs
to exactly one organization → org isolation); separate pool from the ephemeral
pipeline sidecar (UI never blocks the analyzer); no external state — Docker is
the source of truth.
"""

from __future__ import annotations

import logging
import operator
import socket
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from django.conf import settings

from aist.utils import vpn

if TYPE_CHECKING:
    from aist.models import AISTProjectVersion, OrgIntegration

logger = logging.getLogger(__name__)

# Public so the VPN leak sweep can recognise a warm-egress sidecar and leave it to the reaper below.
NAME_PREFIX = "aist-vpn-egress-"
_ACCESS_LOG = "/tmp/tinyproxy-access.log"  # noqa: S108 -- path inside the sidecar container, not this host


# --- Naming (deterministic; derivable without any registry) ------------------

def container_name(vpn_integration_id: int | str) -> str:
    """Warm-egress container name for a VPN integration."""
    return f"{NAME_PREFIX}{vpn_integration_id}"


def proxy_url(vpn_integration_id: int | str) -> str:
    """HTTP CONNECT proxy URL for a VPN integration's warm egress."""
    return f"http://{container_name(vpn_integration_id)}:1080"


# --- Configuration -----------------------------------------------------------

def _idle_ttl_seconds() -> int:
    return int(getattr(settings, "AIST_EGRESS_IDLE_TTL", 900))


def _max_warm() -> int:
    return int(getattr(settings, "AIST_EGRESS_MAX_WARM", 10))


def _allowed_ips() -> list[str]:
    """
    IPs allowed to use the warm-egress tinyproxy (``AIST_ALLOWED_IP`` list).

    The proxy is consumed by the **web** container, so tinyproxy must Allow its
    IP.  This runs in the worker (prewarm), so we resolve the web service name
    via Docker DNS; we also add our own IP (worker) as defence-in-depth.

    Order: explicit ``AIST_EGRESS_ALLOWED_IPS`` override wins; otherwise resolve
    ``AIST_EGRESS_WEB_SERVICE`` (default ``uwsgi``) + own IP.  Never raises.
    """
    override = (getattr(settings, "AIST_EGRESS_ALLOWED_IPS", "") or "").strip()
    if override:
        return [ip for ip in override.replace(",", " ").split() if ip]

    ips: list[str] = []
    web_service = getattr(settings, "AIST_EGRESS_WEB_SERVICE", "uwsgi") or "uwsgi"
    try:
        _n, _a, addrs = socket.gethostbyname_ex(web_service)
        ips.extend(a for a in addrs if a and not a.startswith("127."))
    except OSError:
        logger.debug("egress: could not resolve web service %r for Allow list", web_service, exc_info=True)

    own = vpn.own_eth0_ip()
    if own and own not in ips:
        ips.append(own)
    return ips


# --- VPN resolution ----------------------------------------------------------

def vpn_integration_for_project_version(pv: AISTProjectVersion) -> OrgIntegration | None:
    """
    The active VPN integration a project version's source must be fetched
    through, or ``None`` when no VPN is needed (public SCM).

    Primary wiring is the VPN attached to the SCM integration that owns the repo
    (same one used by validate / list flows via ``scoped_session``).  Falls back
    to the project/org-level VPN default (``resolve_integration``), which also
    honours ``ProjectIntegrationOverride``.
    """
    from aist.integrations.resolver import resolve_integration  # noqa: PLC0415 avoid circular import
    from aist.models import OrgIntegrationType  # noqa: PLC0415

    repo = getattr(pv.project, "repository", None)
    binding = repo.get_binding() if repo is not None else None
    scm_integration = getattr(binding, "org_integration", None) if binding is not None else None
    vpn_integration = getattr(scm_integration, "vpn_integration", None) if scm_integration is not None else None
    if vpn_integration is not None and getattr(vpn_integration, "is_active", False):
        # SECURITY: defense-in-depth cross-org guard (mirrors resolve_integration).
        # A corrupt/hand-inserted SCM binding must never route a project's fetch
        # through another org's VPN credentials.  On mismatch, ignore this wiring
        # and fall through to the org-scoped resolver below.
        proj_org = getattr(pv.project, "organization_id", None)
        vpn_org = getattr(vpn_integration, "organization_id", None)
        if proj_org is not None and vpn_org is not None and vpn_org != proj_org:
            logger.error(
                "egress: cross-org VPN binding ignored — project_version=%s vpn_org=%s project_org=%s",
                getattr(pv, "pk", None), vpn_org, proj_org,
            )
        else:
            return vpn_integration

    resolved = resolve_integration(pv.project, OrgIntegrationType.VPN)
    if resolved is not None and getattr(resolved.integration, "is_active", False):
        return resolved.integration
    return None


def proxy_url_for_project_version(pv: AISTProjectVersion) -> str | None:
    """Deterministic warm-egress proxy URL if the version is VPN-gated, else ``None``."""
    vpn_integration = vpn_integration_for_project_version(pv)
    return proxy_url(vpn_integration.id) if vpn_integration is not None else None


# --- Docker helpers (non-credential; safe to run inline here) ----------------

def _docker() -> str | None:
    from shutil import which  # noqa: PLC0415

    return which("docker")


def _is_running(name: str) -> bool:
    docker_bin = _docker()
    if docker_bin is None:
        return False
    r = subprocess.run(
        [docker_bin, "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True, text=True, check=False,
    )
    return r.returncode == 0 and r.stdout.strip() == "true"


# --- Lifecycle ---------------------------------------------------------------

def ensure_warm(vpn_integration: OrgIntegration) -> str:
    """
    Ensure a warm egress container exists for this VPN integration; return its
    proxy URL.  Idempotent and cheap when already running.  Runs in the worker
    (needs Docker socket).
    """
    vpn_id = vpn_integration.id
    name = container_name(vpn_id)
    if _is_running(name):
        return proxy_url(vpn_id)

    secret = getattr(vpn_integration, "vpn_secret", None)
    if secret is None or not secret.ovpn_content:
        msg = f"egress: VPN integration {vpn_id} has no ovpn_content; cannot start egress"
        raise RuntimeError(msg)

    try:
        url = vpn.start_named_sidecar(name, secret, allowed_ips=_allowed_ips(), log_ctx=f"egress vpn={vpn_id}")
    except RuntimeError:
        # A concurrent prewarm may have won the --name race; reuse if now up.
        if _is_running(name):
            logger.info("egress vpn=%s already started concurrently; reusing", vpn_id)
            return proxy_url(vpn_id)
        raise
    logger.info("egress vpn=%s warm sidecar=%s", vpn_id, name)
    return url


def stop(vpn_integration_id: int | str) -> None:
    """Stop and remove the warm egress container for a VPN integration."""
    vpn.stop_sidecar(container_name(vpn_integration_id))


def list_active() -> list[str]:
    """Names of currently running warm-egress containers."""
    docker_bin = _docker()
    if docker_bin is None:
        return []
    r = subprocess.run(
        [docker_bin, "ps", "--filter", f"name={NAME_PREFIX}", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=False,
    )
    return [n.strip() for n in r.stdout.splitlines() if n.strip().startswith(NAME_PREFIX)]


def _last_used(name: str) -> datetime | None:
    """
    Last-use time of a warm egress, without any shared state: the mtime of the
    tinyproxy connect log (one line per CONNECT).  Falls back to the container's
    StartedAt so a just-started, not-yet-used container is not reaped instantly.
    Returns ``None`` only if both probes fail.
    """
    docker_bin = _docker()
    if docker_bin is None:
        return None
    r = subprocess.run(
        [docker_bin, "exec", name, "stat", "-c", "%Y", _ACCESS_LOG],
        capture_output=True, text=True, check=False,
    )
    if r.returncode == 0 and r.stdout.strip().isdigit():
        return datetime.fromtimestamp(int(r.stdout.strip()), tz=UTC)

    started = subprocess.run(
        [docker_bin, "inspect", "-f", "{{.State.StartedAt}}", name],
        capture_output=True, text=True, check=False,
    )
    raw = started.stdout.strip()
    if started.returncode == 0 and raw:
        try:
            return datetime.fromisoformat(raw).astimezone(UTC)
        except ValueError:
            logger.debug("egress: cannot parse StartedAt=%r for %s", raw, name)
    return None


def reap_idle() -> int:
    """
    Stop warm-egress containers idle longer than ``AIST_EGRESS_IDLE_TTL`` and,
    if more than ``AIST_EGRESS_MAX_WARM`` remain, evict the least-recently-used
    down to the cap.  Returns the number of containers removed.
    """
    now = datetime.now(tz=UTC)
    ttl = _idle_ttl_seconds()
    removed = 0
    survivors: list[tuple[str, datetime]] = []

    for name in list_active():
        last = _last_used(name)
        if last is not None and (now - last).total_seconds() > ttl:
            logger.info("egress: reaping idle sidecar=%s (idle>%ss)", name, ttl)
            vpn.stop_sidecar(name)
            removed += 1
        else:
            survivors.append((name, last or now))

    max_warm = _max_warm()
    if len(survivors) > max_warm:
        survivors.sort(key=operator.itemgetter(1))  # oldest last-use first
        for name, _ in survivors[: len(survivors) - max_warm]:
            logger.info("egress: evicting LRU sidecar=%s (pool cap=%d)", name, max_warm)
            vpn.stop_sidecar(name)
            removed += 1

    if removed:
        logger.info("egress: removed %d warm-egress containers", removed)
    return removed
