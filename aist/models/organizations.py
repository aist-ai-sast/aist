"""Organizations and their integrations."""

from __future__ import annotations

import logging
from contextlib import contextmanager

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from dojo.models import Product_Type
from encrypted_model_fields.fields import EncryptedCharField

from aist.integrations.dast_config import (
    DastConfigError,
    DastIntegrationConfig,
)
from aist.integrations.resolver import ResolvedIntegration

logger = logging.getLogger(__name__)


class Organization(models.Model):

    """
    Simple organization/group entity for AIST projects.

    Project ownership is derived through the organization's Product_Type;
    AISTProject intentionally has no duplicated organization foreign key.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False)
    updated = models.DateTimeField(auto_now=True)

    name = models.CharField(max_length=255, unique=True)
    description = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    product_type = models.OneToOneField(
        Product_Type,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="aist_organization",
    )

    ai_default_filter = models.JSONField(null=True, blank=True, default=None)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def ensure_product_type(self) -> Product_Type:
        if self.product_type_id:
            return self.product_type
        product_type, _ = Product_Type.objects.get_or_create(name=self.name)
        self.product_type = product_type
        self.save(update_fields=["product_type"])
        return product_type


class OrgIntegrationType(models.TextChoices):
    GITLAB = "GITLAB", "GitLab"
    GITHUB = "GITHUB", "GitHub"
    SLACK = "SLACK", "Slack"
    EMAIL = "EMAIL", "Email"
    VPN = "VPN", "VPN"
    CLAUDE_CODE = "CLAUDE_CODE", "Claude Code"
    GERRIT = "GERRIT", "Gerrit"
    GITEA = "GITEA", "Gitea"
    DAST = "DAST", "DAST"


class VpnLinkViolation(models.TextChoices):
    NOT_VPN = "NOT_VPN", "The selected integration is not a VPN integration."
    OTHER_ORGANIZATION = "OTHER_ORGANIZATION", "VPN integration must belong to the same organization."
    INACTIVE = "INACTIVE", "The selected VPN integration is disabled."


def vpn_link_violations(vpn: OrgIntegration, *, organization_id: int | None) -> tuple[VpnLinkViolation, ...]:
    """
    Every reason ``vpn`` cannot route the traffic of something owned by ``organization_id``.

    A VPN link must point to an active VPN-type integration of the same organization; an empty
    result means it does.
    """
    violations = []
    if vpn.integration_type != OrgIntegrationType.VPN:
        violations.append(VpnLinkViolation.NOT_VPN)
    if organization_id is None or vpn.organization_id != organization_id:
        violations.append(VpnLinkViolation.OTHER_ORGANIZATION)
    if not vpn.is_active:
        violations.append(VpnLinkViolation.INACTIVE)
    return tuple(violations)


def resolve_bound_vpn(vpn: OrgIntegration | None, *, organization_id: int | None) -> ResolvedIntegration | None:
    """``vpn`` ready for the sidecar when it may route ``organization_id``'s traffic, else None."""
    if vpn is None:
        return None
    violations = vpn_link_violations(vpn, organization_id=organization_id)
    if violations:
        logger.warning(
            "VPN link ignored (%s): vpn=%s organization=%s",
            ", ".join(violations), vpn.pk, organization_id,
        )
        return None
    return ResolvedIntegration(integration=vpn, config=dict(vpn.config or {}))


class OrgIntegration(models.Model):

    """
    Org-level credential/config store for external integrations.

    ``secret`` is encrypted at rest and never returned by the API.
    ``config`` holds non-secret settings (base_url, channel, from_email …).
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="integrations")
    integration_type = models.CharField(max_length=32, choices=OrgIntegrationType.choices)
    name = models.CharField(max_length=255)
    config = models.JSONField(default=dict, blank=True)
    secret = EncryptedCharField(max_length=4096, blank=True, default="")
    is_active = models.BooleanField(default=True)
    vpn_integration = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        limit_choices_to={"integration_type": "VPN"},
        related_name="dependent_integrations",
        help_text=(
            "VPN integration to route this integration's requests through. "
            "Must be a VPN-type integration in the same organization."
        ),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("organization", "integration_type", "name")]
        ordering = ["organization", "integration_type", "name"]
        constraints = [
            # Claude integrations are single-credential per org by design
            # (OAuth-token model with one active subscription). Other
            # integration types intentionally allow multiple active rows
            # per org (e.g. several GitHub PATs bound to different repos),
            # so the constraint is partial and scoped to CLAUDE_CODE only.
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(integration_type="CLAUDE_CODE", is_active=True),
                name="one_active_claude_integration_per_org",
            ),
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(integration_type="DAST", is_active=True),
                name="one_active_dast_integration_per_org",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.organization.name} / {self.integration_type} / {self.name}"

    def clean(self):
        super().clean()
        errors = {}
        if self.integration_type == OrgIntegrationType.DAST:
            try:
                DastIntegrationConfig.from_snapshot(self.config or {})
            except DastConfigError as exc:
                errors["config"] = str(exc)
        if self.vpn_integration_id is not None:
            violations = vpn_link_violations(self.vpn_integration, organization_id=self.organization_id)
            if violations:
                errors["vpn_integration"] = violations[0].label
        if errors:
            raise ValidationError(errors)

    def get_dast_config(self) -> DastIntegrationConfig:
        if self.integration_type != OrgIntegrationType.DAST:
            msg = "DAST config requested for a non-DAST integration."
            raise DastConfigError(msg)
        return DastIntegrationConfig.from_snapshot(self.config or {})

    @contextmanager
    def scoped_session(self, execution_id: str):
        """
        Context manager — yield a requests.Session with VPN proxy configured
        if vpn_integration may route this integration's traffic.

        Must run in a Celery worker (Docker socket access required for VPN sidecar).
        When no VPN is configured, yields a plain session (no proxy).
        """
        import requests as _requests  # noqa: PLC0415

        from aist.utils.vpn import vpn_sidecar_context  # noqa: PLC0415
        vpn_resolved = resolve_bound_vpn(self.vpn_integration, organization_id=self.organization_id)
        with vpn_sidecar_context(vpn_resolved, execution_id=execution_id) as (_, proxy_url), _requests.Session() as session:
            if proxy_url:
                session.proxies.update({"http": proxy_url, "https": proxy_url})
            yield session


class OrgIntegrationVPNSecret(models.Model):

    """
    Encrypted VPN credentials for an OrgIntegration of type VPN.

    Kept in a separate model so that each credential component is independently
    updatable and VPN-type-specific fields don't pollute OrgIntegration (which is
    shared by all integration types).

    ``ovpn_content`` is the primary field and accepts a full .ovpn file including
    inline <ca>/<cert>/<key> blocks.  The separate cert fields are optional extras:
    the sidecar entrypoint appends them only if the corresponding block is NOT already
    present in ``ovpn_content``, supporting both upload styles without user error.
    """

    integration = models.OneToOneField(
        OrgIntegration,
        on_delete=models.CASCADE,
        related_name="vpn_secret",
    )
    # Full .ovpn file — may already contain inline <ca>/<cert>/<key> blocks.
    # max_length=16384 accommodates enterprise configs with embedded certs.
    ovpn_content = EncryptedCharField(max_length=16384, blank=True, default="")
    # Optional separate PEM blocks (appended only if not already inline in ovpn_content)
    ca_cert = EncryptedCharField(max_length=8192, blank=True, default="")
    client_cert = EncryptedCharField(max_length=8192, blank=True, default="")
    client_key = EncryptedCharField(max_length=8192, blank=True, default="")
    tls_auth_key = EncryptedCharField(max_length=4096, blank=True, default="")
    # Which OpenVPN inline block was used: "tls-auth" (legacy) or "tls-crypt" (modern).
    # Not encrypted — not sensitive. Determines correct block tag on sidecar reassembly.
    tls_key_type = models.CharField(max_length=16, blank=True, default="tls-auth")
    # Optional auth-user-pass credentials
    vpn_username = EncryptedCharField(max_length=512, blank=True, default="")
    vpn_password = EncryptedCharField(max_length=512, blank=True, default="")

    def __str__(self) -> str:
        return f"VPNSecret for {self.integration}"
