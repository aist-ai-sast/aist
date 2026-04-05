from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from aist.models import AISTProject, OrgIntegration, OrgIntegrationType


@dataclass
class ResolvedIntegration:
    integration: OrgIntegration
    # Merged config: org integration config overridden by project-level config_override.
    config: dict


def resolve_integration(project: AISTProject, integration_type: OrgIntegrationType) -> ResolvedIntegration | None:
    """
    Return the effective OrgIntegration for a given project and type.

    Resolution order:
    1. ProjectIntegrationOverride for this project+type (explicit binding + optional config override).
    2. First active OrgIntegration of this type for the project's organization (org default).

    Returns None if no integration is configured for the org.
    """
    from aist.models import OrgIntegration, ProjectIntegrationOverride  # noqa: PLC0415 avoid circular import

    override = (
        ProjectIntegrationOverride.objects.filter(
            project=project,
            integration_type=integration_type,
        )
        .select_related("org_integration")
        .first()
    )

    if override is not None:
        if override.is_disabled:
            return None  # explicitly disabled for this project — skip org default
        if override.org_integration and override.org_integration.is_active:
            integration = override.org_integration
            # SECURITY: defense-in-depth — verify org ownership even if the view already
            # checked it.  Guards against corrupt or manually-inserted cross-org overrides
            # that could route a project's traffic through another org's VPN credentials.
            if integration.organization_id != project.organization_id:
                logger.error(
                    "resolve_integration: cross-org override detected — "
                    "project=%s override=%s integration_org=%s project_org=%s; "
                    "falling back to org default",
                    project.pk, override.pk,
                    integration.organization_id, project.organization_id,
                )
                # fall through to org default below
            else:
                effective_config = {**integration.config, **override.config_override}
                return ResolvedIntegration(integration=integration, config=effective_config)
        # override exists but no org_integration (or cross-org detected) → fall through to org default

    if project.organization_id is None:
        return None

    integration = OrgIntegration.objects.filter(
        organization_id=project.organization_id,
        integration_type=integration_type,
        is_active=True,
    ).order_by("created").first()

    if integration is None:
        return None

    return ResolvedIntegration(integration=integration, config=dict(integration.config))
