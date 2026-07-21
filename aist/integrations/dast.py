"""
DAST integration gateway as a first-class ``OrgIntegration`` — single concentrator.

This module is the **only** place in the codebase that knows the mapping between an
``OrgIntegration(type=DAST)`` and the concrete environment variable names the DAST analyzer
container expects (``DAST_GATEWAY_URL`` / ``DAST_INTEGRATOR_TOKEN``). Other modules
(``run_sast_pipeline``, ``configure_project_run_analyses``, the analyzer runner) deal with a
generic ``additional_env`` dict and stay integration-agnostic — mirrors the layout of
``aist/integrations/claude.py`` (I1-style invariant: this literal env-var mapping lives only here).

The gateway URL is non-secret config; the integrator token is the encrypted secret. Both are
required together — a URL with no token (or vice versa) means the integration isn't usable, so
``dast_env`` returns nothing rather than a half-configured env.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import requests

from aist.integrations.resolver import resolve_integration
from aist.models import OrgIntegrationType

if TYPE_CHECKING:
    from aist.models import AISTProject, OrgIntegration

logger = logging.getLogger(__name__)

ENV_GATEWAY_URL = "DAST_GATEWAY_URL"
ENV_INTEGRATOR_TOKEN = "DAST_INTEGRATOR_TOKEN"  # noqa: S105  (env var name, not a secret value)

_PING_PATH = "/integrations/v1/ping"
_PROBE_TIMEOUT_SECONDS = 10


def dast_env(project: AISTProject) -> dict[str, str]:
    """
    Resolve the project's DAST integration into the analyzer's env dict.

    Returns an empty dict if no integration is configured, the integration is inactive, the
    token is empty, or ``config.gateway_url`` is missing — callers treat an empty dict as "DAST
    not available for this project" (the analyzer plugin surfaces this as a clear
    `[INFO] DAST not configured` line rather than attempting a request with no credentials).
    """
    resolved = resolve_integration(project, OrgIntegrationType.DAST)
    if resolved is None:
        return {}
    token = (resolved.integration.secret or "").strip()
    gateway_url = (resolved.config.get("gateway_url") or "").strip()
    if not token or not gateway_url:
        return {}
    return {ENV_GATEWAY_URL: gateway_url, ENV_INTEGRATOR_TOKEN: token}


def probe_dast_gateway(integration: OrgIntegration) -> tuple[bool, str]:
    """
    Cheap connectivity + auth check against the DAST gateway's ``GET /integrations/v1/ping`` —
    launches no DAST run (see the gateway's own docs: ping is the deliberately side-effect-free
    smoke test). Honours ``integration.vpn_integration`` via ``scoped_session`` — if the org
    routes this integration's traffic through a VPN sidecar, this probe will too. Mirrors
    ``probe_claude_token``'s shape (aist/integrations/claude.py).

    The detail string MUST NOT contain the token: constructed defensively from status codes
    and exception type names only, never from the request/response body.
    """
    token = (integration.secret or "").strip()
    gateway_url = (integration.config or {}).get("gateway_url", "").strip()
    if not token:
        return False, "no token stored"
    if not gateway_url:
        return False, "no gateway_url configured"

    headers = {"Authorization": f"Bearer {token}"}
    try:
        with integration.scoped_session(execution_id=f"dast-probe-{integration.pk}") as session:
            response = session.get(
                f"{gateway_url.rstrip('/')}{_PING_PATH}",
                headers=headers,
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
    except requests.ConnectionError:
        logger.warning("DAST probe[%s]: connection error to gateway", integration.pk)
        return False, "unreachable: connection error"
    except requests.Timeout:
        logger.warning("DAST probe[%s]: timeout", integration.pk)
        return False, "unreachable: timeout"
    except Exception as exc:
        # Defensive — exception type name only, never repr(exc) which could include
        # the request URL/headers in some libraries.
        logger.exception("DAST probe[%s]: unexpected error", integration.pk)
        return False, f"unreachable: {type(exc).__name__}"

    if response.status_code == 200:
        return True, "gateway reachable and token accepted"
    if response.status_code in {401, 403}:
        return False, f"token rejected (HTTP {response.status_code})"
    return False, f"unexpected response (HTTP {response.status_code})"
