"""Credential probe of a VPN integration."""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aist.models import OrgIntegration

logger = logging.getLogger(__name__)


def _parse_remote_from_ovpn(ovpn_content: str) -> str | None:
    """Return the hostname from the first 'remote <host> [port] [proto]' line."""
    for line in ovpn_content.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] == "remote":
            return parts[1]
    return None


def probe_vpn(integration: OrgIntegration) -> tuple[bool, str]:
    """
    Validate a VPN integration by pinging the VPN server endpoint.

    Uses config.ping_target if set; otherwise parses the 'remote' directive from
    the .ovpn content.  Does NOT establish a VPN tunnel — this is a lightweight
    reachability check only.
    """
    vpn_secret = getattr(integration, "vpn_secret", None)
    if not vpn_secret or not vpn_secret.ovpn_content:
        return False, "No VPN configuration stored. Upload an .ovpn file first."

    config = integration.config or {}
    ping_target = config.get("ping_target") or _parse_remote_from_ovpn(vpn_secret.ovpn_content)
    if not ping_target:
        return False, (
            "Cannot determine server address. "
            "Set config.ping_target or add a 'remote <host>' directive in the .ovpn content."
        )

    try:
        ping_bin = shutil.which("ping")
        if ping_bin is None:
            return False, "Ping binary is not available on the server."
        result = subprocess.run(
            [ping_bin, "-c", "1", "-W", "5", ping_target],
            capture_output=True,
            timeout=10,
            check=False,
        )
        is_reachable = result.returncode == 0
        detail = (
            f"{ping_target} is reachable."
            if is_reachable
            else f"{ping_target} unreachable (ping returned {result.returncode})."
        )
    except subprocess.TimeoutExpired:
        return False, f"Ping to {ping_target} timed out."
    except Exception as exc:
        logger.exception("VPN reachability check failed for %s", ping_target)
        return False, f"Reachability check failed ({type(exc).__name__}) — see server logs."
    else:
        return is_reachable, detail
