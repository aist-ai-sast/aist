"""SCM provider connections: the REST helpers shared by SCM tasks and each provider's credential probe."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import gitlab
import pygerrit2

if TYPE_CHECKING:
    import requests

    from aist.models import OrgIntegration

logger = logging.getLogger(__name__)


def gerrit_rest(integration: OrgIntegration, session: requests.Session) -> tuple[pygerrit2.GerritRestAPI, str]:
    """Build a pygerrit2 REST client for an integration over the given session."""
    base_url = ((integration.config or {}).get("base_url") or "").rstrip("/")
    username = ((integration.config or {}).get("username") or "").strip()
    password = (integration.secret or "").strip()
    auth = pygerrit2.HTTPBasicAuth(username, password) if username and password else None
    rest = pygerrit2.GerritRestAPI(url=base_url, auth=auth)
    rest.session = session
    return rest, base_url


def gitea_headers(integration: OrgIntegration) -> dict:
    """Gitea's documented auth header — not Bearer, not Basic."""
    token = (integration.secret or "").strip()
    return {"Authorization": f"token {token}"} if token else {}


def probe_gitlab(integration: OrgIntegration) -> tuple[bool, str]:
    """Authenticate the stored token against the GitLab instance, over the integration's route."""
    base_url = (integration.config or {}).get("base_url") or "https://gitlab.com"
    try:
        with integration.scoped_session(execution_id=f"validate-{integration.pk}") as session:
            gl = gitlab.Gitlab(base_url, private_token=integration.secret or None, session=session)
            gl.auth()
    except Exception as exc:
        logger.exception("Integration[%s] GITLAB validation error", integration.pk)
        return False, f"Validation failed ({type(exc).__name__}) — see server logs."
    else:
        return True, ""


def probe_github(integration: OrgIntegration) -> tuple[bool, str]:
    """GitHub App auth is validated at installation time; no stored secret to test."""
    return True, "GitHub uses App-level auth; no credential stored."


def probe_gerrit(integration: OrgIntegration) -> tuple[bool, str]:
    """Read the authenticated Gerrit account, over the integration's route."""
    if not ((integration.config or {}).get("base_url") or "").strip():
        return False, "Gerrit integration requires a base_url in config."
    try:
        with integration.scoped_session(execution_id=f"validate-{integration.pk}") as session:
            rest, _base_url = gerrit_rest(integration, session)
            rest.get("/accounts/self")
    except Exception as exc:
        logger.exception("Integration[%s] GERRIT validation error", integration.pk)
        return False, f"Validation failed ({type(exc).__name__}) — see server logs."
    else:
        return True, ""


def probe_gitea(integration: OrgIntegration) -> tuple[bool, str]:
    """
    Search one repository, over the integration's route.

    ``/api/v1/repos/search`` needs only the "read:repository" scope the integration uses;
    ``/api/v1/user`` would also need "read:user".
    """
    base_url = ((integration.config or {}).get("base_url") or "").rstrip("/")
    if not base_url:
        return False, "Gitea integration requires a base_url in config."
    try:
        with integration.scoped_session(execution_id=f"validate-{integration.pk}") as session:
            resp = session.get(
                f"{base_url}/api/v1/repos/search",
                headers=gitea_headers(integration),
                params={"limit": 1},
                timeout=15,
            )
            resp.raise_for_status()
    except Exception as exc:
        logger.exception("Integration[%s] GITEA validation error", integration.pk)
        return False, f"Validation failed ({type(exc).__name__}) — see server logs."
    else:
        return True, ""
