"""
DAST integration gateway validation for a first-class ``OrgIntegration``.

Runtime credentials are intentionally not converted to analyzer environment variables. The
versioned DAST executor receives secrets only at its connector transport boundary.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aist.integrations.dast_gateway_client import DastGatewayClientError, scoped_dast_gateway_client

if TYPE_CHECKING:
    from aist.models import OrgIntegration

logger = logging.getLogger(__name__)


def probe_dast_gateway(integration: OrgIntegration) -> tuple[bool, str]:
    """
    Cheap connectivity + auth check against the DAST gateway's ``GET /integrations/v2/ping`` —
    launches no DAST run (see the gateway's own docs: ping is the deliberately side-effect-free
    smoke test). Honours ``integration.vpn_integration`` via ``scoped_session`` — if the org
    routes this integration's traffic through a VPN sidecar, this probe will too. Mirrors
    ``probe_claude_token``'s shape (aist/integrations/claude.py).

    The detail string MUST NOT contain the token: constructed defensively from status codes
    and exception type names only, never from the request/response body.
    """
    try:
        with scoped_dast_gateway_client(
            integration,
            execution_id=f"dast-probe-{integration.pk}",
        ) as client:
            client.ping()
    except DastGatewayClientError as exc:
        logger.warning("DAST probe[%s] failed with code %s", integration.pk, exc.code)
        return False, exc.code
    return True, "gateway reachable and token accepted"
