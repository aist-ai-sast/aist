"""Credential validation of an organization integration, dispatched to its provider's probe."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aist.integrations.build_env import probe_build_env
from aist.integrations.claude import probe_claude_token
from aist.integrations.dast import probe_dast_gateway
from aist.integrations.notification import probe_email, probe_slack
from aist.integrations.scm import probe_gerrit, probe_gitea, probe_github, probe_gitlab
from aist.integrations.vpn import probe_vpn
from aist.models import OrgIntegrationType

if TYPE_CHECKING:
    from collections.abc import Callable

    from aist.models import OrgIntegration

INTEGRATION_PROBES: dict[OrgIntegrationType, Callable[[OrgIntegration], tuple[bool, str]]] = {
    OrgIntegrationType.GITLAB: probe_gitlab,
    OrgIntegrationType.SLACK: probe_slack,
    OrgIntegrationType.GITHUB: probe_github,
    OrgIntegrationType.GERRIT: probe_gerrit,
    OrgIntegrationType.GITEA: probe_gitea,
    OrgIntegrationType.EMAIL: probe_email,
    OrgIntegrationType.BUILD_ENV: probe_build_env,
    OrgIntegrationType.VPN: probe_vpn,
    OrgIntegrationType.CLAUDE_CODE: probe_claude_token,
    OrgIntegrationType.DAST: probe_dast_gateway,
}


def validate_integration(integration: OrgIntegration) -> tuple[bool, str]:
    """Return (valid, detail) from the probe of the integration's type, without raising on credential failures."""
    probe = INTEGRATION_PROBES.get(integration.integration_type)
    if probe is None:
        return False, f"No validator for integration type {integration.integration_type}"
    return probe(integration)
