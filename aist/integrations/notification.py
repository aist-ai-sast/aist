"""Credential probes of the notification integrations (Slack, email)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aist.notifications import AISTSlackNotificationManager

if TYPE_CHECKING:
    from aist.models import OrgIntegration

logger = logging.getLogger(__name__)


def probe_slack(integration: OrgIntegration) -> tuple[bool, str]:
    """Test the stored Slack bot token."""
    try:
        ok = AISTSlackNotificationManager().test_token(integration.secret or "")
    except Exception as exc:
        logger.exception("Integration[%s] SLACK validation error", integration.pk)
        return False, f"Validation failed ({type(exc).__name__}) — see server logs."
    else:
        return ok, "" if ok else "Slack auth failed"


def probe_email(integration: OrgIntegration) -> tuple[bool, str]:
    """Email configuration has no remote check."""
    return True, "Email configuration is not automatically validated."
