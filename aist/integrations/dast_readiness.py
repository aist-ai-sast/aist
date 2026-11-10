from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from django.utils import timezone

from aist.integrations.dast_config import DastBindingParameters, DastConfigError, DastTargetSnapshot
from aist.models import (
    DastIntegrationState,
    DastIntegrationValidationState,
    DastProjectBinding,
    OrgIntegrationType,
    OrgIntegrationVPNSecret,
)
from aist.models.organizations import VpnLinkViolation, vpn_link_violations
from aist.pipeline_args import DastPipelineArguments
from aist.utils.pipeline_imports import _import_sast_pipeline_package

_import_sast_pipeline_package()

from pipeline.dast.contracts import CONTRACT_MAJOR  # type: ignore[import-not-found]  # noqa: E402

DAST_CATALOG_MAX_AGE = timedelta(hours=24)


class DastReadinessCode(StrEnum):
    INTEGRATION_TYPE_INVALID = "INTEGRATION_TYPE_INVALID"
    INTEGRATION_INACTIVE = "INTEGRATION_INACTIVE"
    INTEGRATION_TOKEN_MISSING = "INTEGRATION_TOKEN_MISSING"  # noqa: S105 -- reason code, not a token
    INTEGRATION_CONFIG_INVALID = "INTEGRATION_CONFIG_INVALID"
    VALIDATION_NOT_READY = "VALIDATION_NOT_READY"
    CONTRACT_INCOMPATIBLE = "CONTRACT_INCOMPATIBLE"
    CATALOG_NOT_SYNCED = "CATALOG_NOT_SYNCED"
    CATALOG_STALE = "CATALOG_STALE"
    CATALOG_SYNC_FAILED = "CATALOG_SYNC_FAILED"
    BINDING_DISABLED = "BINDING_DISABLED"
    TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"
    TARGET_CATALOG_INVALID = "TARGET_CATALOG_INVALID"
    TARGET_CONTRACT_INCOMPATIBLE = "TARGET_CONTRACT_INCOMPATIBLE"
    BINDING_PARAMETERS_INVALID = "BINDING_PARAMETERS_INVALID"
    SOURCE_REPOSITORY_UNAVAILABLE = "SOURCE_REPOSITORY_UNAVAILABLE"
    VPN_TYPE_INVALID = "VPN_TYPE_INVALID"
    VPN_ORGANIZATION_MISMATCH = "VPN_ORGANIZATION_MISMATCH"
    VPN_INACTIVE = "VPN_INACTIVE"
    VPN_CREDENTIALS_MISSING = "VPN_CREDENTIALS_MISSING"
    VPN_USER_PASSWORD_INCOMPLETE = "VPN_USER_PASSWORD_INCOMPLETE"  # noqa: S105 -- reason code
    SOURCE_VERSION_INVALID = "SOURCE_VERSION_INVALID"
    LAUNCH_PARAMETERS_INVALID = "LAUNCH_PARAMETERS_INVALID"
    CAPABILITY_SNAPSHOT_STALE = "CAPABILITY_SNAPSHOT_STALE"


@dataclass(frozen=True, slots=True)
class DastReadinessIssue:
    code: DastReadinessCode
    detail: str

    def to_snapshot(self) -> dict[str, str]:
        return {"code": self.code.value, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class DastReadinessResult:
    ready: bool
    issues: tuple[DastReadinessIssue, ...]
    checked_at: datetime

    def to_snapshot(self) -> dict:
        return {
            "ready": self.ready,
            "issues": [issue.to_snapshot() for issue in self.issues],
            "checked_at": self.checked_at.isoformat(),
        }


_VPN_LINK_ISSUES = {
    VpnLinkViolation.NOT_VPN: DastReadinessCode.VPN_TYPE_INVALID,
    VpnLinkViolation.OTHER_ORGANIZATION: DastReadinessCode.VPN_ORGANIZATION_MISMATCH,
    VpnLinkViolation.INACTIVE: DastReadinessCode.VPN_INACTIVE,
}


def _add_issue(issues: list[DastReadinessIssue], code: DastReadinessCode, detail: str) -> None:
    """Record ``code`` once; the first detail given for it is kept."""
    if all(issue.code != code for issue in issues):
        issues.append(DastReadinessIssue(code=code, detail=detail))


def check_dast_binding_readiness(
    binding: DastProjectBinding,
    *,
    now: datetime | None = None,
    catalog_max_age: timedelta = DAST_CATALOG_MAX_AGE,
) -> DastReadinessResult:
    """Evaluate one binding without network I/O or secret-bearing output."""
    checked_at = now or timezone.now()
    issues: list[DastReadinessIssue] = []

    def add(code: DastReadinessCode, detail: str) -> None:
        _add_issue(issues, code, detail)

    target = binding.target
    integration = target.integration
    if integration.integration_type != OrgIntegrationType.DAST:
        add(DastReadinessCode.INTEGRATION_TYPE_INVALID, "Binding target is not owned by a DAST integration.")
    if not integration.is_active:
        add(DastReadinessCode.INTEGRATION_INACTIVE, "The DAST integration is disabled.")
    if not integration.secret:
        add(DastReadinessCode.INTEGRATION_TOKEN_MISSING, "The DAST integration has no API token.")

    try:
        integration.get_dast_config()
    except DastConfigError:
        add(DastReadinessCode.INTEGRATION_CONFIG_INVALID, "The stored DAST integration configuration is invalid.")

    try:
        state = integration.dast_state
    except DastIntegrationState.DoesNotExist:
        state = None
    if state is None or state.validation_state != DastIntegrationValidationState.READY:
        add(DastReadinessCode.VALIDATION_NOT_READY, "The DAST integration has not passed connection validation.")
    if state is None or _contract_major(state.contract_version) != CONTRACT_MAJOR:
        add(
            DastReadinessCode.CONTRACT_INCOMPATIBLE,
            f"The validated DAST API contract is not compatible with v{CONTRACT_MAJOR}.",
        )
    if state is None or not state.capabilities_etag or state.capabilities_synced_at is None:
        add(DastReadinessCode.CATALOG_NOT_SYNCED, "The DAST target catalog has not been synchronized.")
    elif checked_at - state.capabilities_synced_at > catalog_max_age:
        add(DastReadinessCode.CATALOG_STALE, "The DAST target catalog is older than the allowed freshness window.")
    if state is not None and state.sync_error_code:
        add(DastReadinessCode.CATALOG_SYNC_FAILED, "The most recent DAST target catalog synchronization failed.")

    if not binding.enabled:
        add(DastReadinessCode.BINDING_DISABLED, "This DAST target binding is disabled.")
    if not target.is_available:
        add(DastReadinessCode.TARGET_UNAVAILABLE, "The selected DAST target is no longer available.")

    target_snapshot = None
    try:
        target_snapshot = target.get_snapshot()
    except DastConfigError:
        add(
            DastReadinessCode.TARGET_CATALOG_INVALID,
            "The selected target schema or provider defaults are invalid; synchronize the catalog again.",
        )
    if target_snapshot is not None:
        if _contract_major(target_snapshot.contract_revision) != CONTRACT_MAJOR:
            add(
                DastReadinessCode.TARGET_CONTRACT_INCOMPATIBLE,
                f"The selected target does not use DAST contract v{CONTRACT_MAJOR}.",
            )
        try:
            binding.get_parameters()
        except DastConfigError:
            add(
                DastReadinessCode.BINDING_PARAMETERS_INVALID,
                "The saved target parameters no longer match the synchronized provider schema.",
            )
        if target_snapshot.launch_requirements.requires_repository() and (
            binding.source_repo_key not in target_snapshot.repository_keys
        ):
            add(
                DastReadinessCode.SOURCE_REPOSITORY_UNAVAILABLE,
                "The selected source repository is not advertised by the DAST target.",
            )
    if integration.vpn_integration_id is not None:
        vpn = integration.vpn_integration
        for violation in vpn_link_violations(vpn, organization_id=integration.organization_id):
            add(_VPN_LINK_ISSUES[violation], violation.label)
        try:
            vpn_secret = vpn.vpn_secret
        except OrgIntegrationVPNSecret.DoesNotExist:
            vpn_secret = None
        if vpn_secret is None or not vpn_secret.ovpn_content:
            add(DastReadinessCode.VPN_CREDENTIALS_MISSING, "The selected VPN has no OpenVPN credentials.")
        elif bool(vpn_secret.vpn_username) != bool(vpn_secret.vpn_password):
            add(
                DastReadinessCode.VPN_USER_PASSWORD_INCOMPLETE,
                "The selected VPN must provide both username and password or neither.",
            )

    return DastReadinessResult(ready=not issues, issues=tuple(issues), checked_at=checked_at)


def check_dast_launch_readiness(arguments, *, now: datetime | None = None) -> DastReadinessResult:
    """Evaluate a complete ephemeral or saved DAST launch input without network I/O."""
    if not isinstance(arguments.payload, DastPipelineArguments):
        msg = "DAST launch readiness requires DAST pipeline arguments."
        raise TypeError(msg)
    payload = arguments.payload
    base = check_dast_binding_readiness(payload.binding, now=now)
    issues = list(base.issues)

    def add(code: DastReadinessCode, detail: str) -> None:
        _add_issue(issues, code, detail)

    violations = payload.binding.launch_violations(
        project_id=arguments.project.pk,
        trigger_version=payload.trigger_project_version,
    )
    if violations:
        add(DastReadinessCode.SOURCE_VERSION_INVALID, violations[0].label)
    try:
        current_target = payload.binding.target.get_snapshot()
        DastBindingParameters.from_snapshot(payload.parameters, target=current_target)
        launch_target = DastTargetSnapshot.from_snapshot(payload.capability)
        if launch_target.revision_identity != current_target.revision_identity:
            add(
                DastReadinessCode.CAPABILITY_SNAPSHOT_STALE,
                "The DAST target capability changed; rebuild the launch input.",
            )
    except DastConfigError:
        add(
            DastReadinessCode.LAUNCH_PARAMETERS_INVALID,
            "The launch parameters no longer match the DAST target schema.",
        )
    checked_at = now or timezone.now()
    return DastReadinessResult(ready=not issues, issues=tuple(issues), checked_at=checked_at)


def _contract_major(version: str) -> int | None:
    try:
        return int(version.split(".", maxsplit=1)[0])
    except (AttributeError, TypeError, ValueError):
        return None
