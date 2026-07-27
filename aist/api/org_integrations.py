from __future__ import annotations

import logging
import re
import shutil
import subprocess

from django.db import IntegrityError, transaction
from django.http import Http404
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_field
from rest_framework import serializers, status
from rest_framework.permissions import SAFE_METHODS
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from aist.api.schema import AISTApiTag
from aist.authz import Action, AISTAPIView, ResourcePolicy
from aist.execution.observability import AuditContext, audit_event
from aist.integrations.dast_config import (
    DastConfigError,
    DastIntegrationConfig,
    DastOnboardingBundle,
)
from aist.integrations.dast_validation import (
    mark_dast_validation_pending,
    mark_vpn_linked_dast_validations_pending,
    schedule_dast_validation,
)
from aist.models import (
    AISTProject,
    DastOnboardingBundleUse,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    OrgIntegrationVPNSecret,
    ProjectIntegrationOverride,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OVPN helpers
# ---------------------------------------------------------------------------

# Maps OpenVPN XML-like tag names to VPNSecret model field names.
_OVPN_TAG_TO_FIELD: dict[str, str] = {
    "ca": "ca_cert",
    "cert": "client_cert",
    "key": "client_key",
    "tls-auth": "tls_auth_key",
    "tls-crypt": "tls_auth_key",  # tls-crypt uses same storage field
    "tls-crypt-v2": "tls_auth_key",  # OpenVPN 2.5+ per-client wrapped keys
}


def _split_ovpn_pem_blocks(ovpn_content: str) -> tuple[str, dict[str, str]]:
    """
    Extract inline PEM/key blocks from an .ovpn file.

    Returns ``(cleaned_config, extracted)`` where:
    - ``cleaned_config`` — original content with the extracted blocks removed
      (only connection directives remain; safe to log)
    - ``extracted`` — dict mapping model field names to the block content,
      e.g. ``{"ca_cert": "...", "client_key": "..."}``

    Also sets ``extracted["tls_key_type"]`` to "tls-crypt", "tls-crypt-v2", or "tls-auth"
    so the entrypoint can reconstruct the correct block tag.  The distinction
    matters: ``tls-crypt`` and ``tls-auth`` are different OpenVPN protocols and
    the server will silently drop packets if the wrong one is used.

    When multiple tags map to the same field (tls-auth / tls-crypt / tls-crypt-v2)
    the last matched value wins, but in practice a valid .ovpn has only one of them.
    """
    extracted: dict[str, str] = {}
    cleaned = ovpn_content
    for tag, field in _OVPN_TAG_TO_FIELD.items():
        pattern = re.compile(
            rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>",
            re.DOTALL | re.IGNORECASE,
        )
        match = pattern.search(cleaned)
        if match:
            extracted[field] = match.group(1).strip()
            if field == "tls_auth_key":
                # Record which tag was used so the sidecar entrypoint can
                # reconstruct <tls-auth> vs <tls-crypt> vs <tls-crypt-v2> correctly.
                extracted["tls_key_type"] = tag
                if tag != "tls-auth":
                    # tls-crypt and tls-crypt-v2 do not use key-direction; remove it
                    # so the entrypoint does not need to strip it from the base config.
                    cleaned = re.sub(r"\nkey-direction\s+\d+", "", cleaned)
            cleaned = pattern.sub("", cleaned)
    # Collapse runs of blank lines left after block removal
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, extracted


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


class DastOnboardingBundleSerializer(serializers.Serializer):

    """Strict write boundary for a versioned, one-shot DAST onboarding bundle."""

    bundle_version = serializers.IntegerField()
    gateway_url = serializers.CharField()
    ca_bundle = serializers.CharField(allow_blank=True, trim_whitespace=False)
    contract_major = serializers.IntegerField()
    integrator_public_id = serializers.CharField()
    server_fingerprint = serializers.CharField()
    token = serializers.CharField(
        write_only=True,
        trim_whitespace=False,
        style={"input_type": "password"},
    )

    def __repr__(self):
        return f"{self.__class__.__name__}(token=<write-only>)"

    def to_internal_value(self, data):
        try:
            DastOnboardingBundle.from_mapping(data)
        except DastConfigError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return super().to_internal_value(data)

    def validate(self, attrs):
        try:
            bundle = DastOnboardingBundle.from_mapping(attrs)
        except DastConfigError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        self.bundle = bundle
        return {
            **bundle.to_safe_snapshot(),
            "token": bundle.token,
        }


class DastIntegrationOnboardingSerializer(serializers.Serializer):

    """Create/update boundary that keeps bundle parsing and secret extraction together."""

    name = serializers.CharField(max_length=255, required=False, default="DAST Gateway")
    vpn_integration_id = serializers.IntegerField(required=False, allow_null=True, write_only=True)
    bundle = DastOnboardingBundleSerializer(write_only=True)

    def to_internal_value(self, data):
        if (
            not isinstance(data, dict)
            or "bundle" not in data
            or set(data) - {"name", "vpn_integration_id", "bundle"}
        ):
            msg = "A DAST onboarding bundle is required and unknown fields are not allowed."
            raise serializers.ValidationError({"non_field_errors": [msg]})
        return super().to_internal_value(data)

    def validate(self, attrs):
        bundle = DastOnboardingBundle.from_mapping(attrs["bundle"])
        attrs["parsed_bundle"] = bundle
        if "vpn_integration_id" in attrs:
            vpn_id = attrs.pop("vpn_integration_id")
            vpn = None
            if vpn_id is not None:
                organization = self.context["organization"]
                vpn = OrgIntegration.objects.filter(
                    pk=vpn_id,
                    organization=organization,
                    integration_type=OrgIntegrationType.VPN,
                    is_active=True,
                ).first()
                if vpn is None:
                    raise serializers.ValidationError({"vpn_integration_id": "Active same-organization VPN required."})
            attrs["vpn_integration"] = vpn
        return attrs

    def create(self, validated_data):
        bundle = validated_data.pop("parsed_bundle")
        validated_data.pop("bundle")
        integration = OrgIntegration.objects.create(
            organization=self.context["organization"],
            integration_type=OrgIntegrationType.DAST,
            config=bundle.config.to_snapshot(),
            secret=bundle.token,
            created_by=self.context["requester"],
            is_active=True,
            **validated_data,
        )
        schedule_dast_validation(integration)
        return integration

    def update(self, instance, validated_data):
        bundle = validated_data.pop("parsed_bundle")
        validated_data.pop("bundle")
        instance.config = bundle.config.to_snapshot()
        instance.secret = bundle.token
        for field in ("name", "vpn_integration"):
            if field in validated_data:
                setattr(instance, field, validated_data[field])
        instance.save(update_fields=["config", "secret", "name", "vpn_integration", "updated"])
        schedule_dast_validation(instance)
        return instance

    def to_representation(self, instance):
        return OrgIntegrationSerializer(instance).data


class DastTokenRotationSerializer(serializers.Serializer):

    token = serializers.CharField(
        write_only=True,
        trim_whitespace=False,
        max_length=DastOnboardingBundle.MAX_TOKEN_BYTES,
        style={"input_type": "password"},
    )

    def __repr__(self):
        return f"{self.__class__.__name__}(token=<write-only>)"

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) != {"token"}:
            msg = "A token rotation object with no unknown fields is required."
            raise serializers.ValidationError({"non_field_errors": [msg]})
        return super().to_internal_value(data)

    def validate_token(self, value):
        if not value or value != value.strip():
            msg = "token must be non-empty and contain no surrounding whitespace."
            raise serializers.ValidationError(msg)
        return value


class OrgIntegrationVPNSecretSerializer(serializers.ModelSerializer):

    """
    Serializer for OrgIntegrationVPNSecret.
    All credential fields are write-only; only boolean presence indicators are readable.
    On PATCH, fields not present in the request body are preserved unchanged.
    """

    ovpn_content = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        style={"input_type": "password"},
        help_text="Full .ovpn file content (inline <ca>/<cert>/<key> blocks supported).",
    )
    ca_cert = serializers.CharField(write_only=True, required=False, allow_blank=True)
    client_cert = serializers.CharField(write_only=True, required=False, allow_blank=True)
    client_key = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        style={"input_type": "password"},
    )
    tls_auth_key = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        style={"input_type": "password"},
    )
    vpn_username = serializers.CharField(write_only=True, required=False, allow_blank=True)
    vpn_password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        style={"input_type": "password"},
    )
    # Not sensitive — indicates whether the uploaded .ovpn used tls-auth or tls-crypt.
    # Read-write so that manual PATCH requests can correct it if needed.
    # No default here: the value is always derived from the uploaded .ovpn via
    # _split_ovpn_pem_blocks.  A default="tls-auth" would prevent the extracted
    # "tls-crypt" from being applied (DRF always injects the default into attrs,
    # making `if field not in attrs` always false for this field).
    tls_key_type = serializers.CharField(required=False, allow_blank=True)
    has_ovpn_content = serializers.SerializerMethodField()
    has_client_cert = serializers.SerializerMethodField()
    has_client_key = serializers.SerializerMethodField()
    has_username = serializers.SerializerMethodField()

    def get_has_ovpn_content(self, obj) -> bool:
        return bool(obj.ovpn_content)

    def get_has_client_cert(self, obj) -> bool:
        return bool(obj.client_cert)

    def get_has_client_key(self, obj) -> bool:
        return bool(obj.client_key)

    def get_has_username(self, obj) -> bool:
        return bool(obj.vpn_username)

    def validate(self, attrs):
        ovpn = attrs.get("ovpn_content", "")
        if ovpn:
            cleaned, extracted = _split_ovpn_pem_blocks(ovpn)
            attrs["ovpn_content"] = cleaned
            # Populate separate cert fields from the inline blocks only when
            # the caller has not explicitly supplied those fields themselves.
            # Fields absent from the request are not present in attrs at all
            # (DRF omits them during to_internal_value for optional fields).
            # tls_key_type is always overwritten from the parsed file — it must
            # reflect the actual tag (<tls-auth> vs <tls-crypt>) found in the
            # uploaded .ovpn regardless of any previously stored value.
            for field, value in extracted.items():
                if field not in attrs or field == "tls_key_type":
                    attrs[field] = value
        return attrs

    def update(self, instance, validated_data):
        # On PATCH: preserve fields not explicitly provided in the request body,
        # EXCEPT for fields derived from ovpn_content when it was uploaded —
        # those must be saved together with the new ovpn_content.
        ovpn_provided = "ovpn_content" in self.initial_data
        # Fields extracted from ovpn_content during validate():
        ovpn_derived = {"ca_cert", "client_cert", "client_key", "tls_auth_key", "tls_key_type"}
        secret_fields = (
            "ovpn_content",
            "ca_cert",
            "client_cert",
            "client_key",
            "tls_auth_key",
            "tls_key_type",
            "vpn_username",
            "vpn_password",
        )
        for field in secret_fields:
            if field not in self.initial_data:
                # Keep derived fields when they came from a freshly uploaded ovpn_content.
                if ovpn_provided and field in ovpn_derived:
                    continue
                validated_data.pop(field, None)
        return super().update(instance, validated_data)

    class Meta:
        model = OrgIntegrationVPNSecret
        fields = [
            "ovpn_content",
            "ca_cert",
            "client_cert",
            "client_key",
            "tls_auth_key",
            "tls_key_type",
            "vpn_username",
            "vpn_password",
            "has_ovpn_content",
            "has_client_cert",
            "has_client_key",
            "has_username",
        ]


class DastIntegrationStateSerializer(serializers.Serializer):

    validation_state = serializers.CharField()
    validation_error_code = serializers.CharField(allow_blank=True)
    contract_version = serializers.CharField(allow_blank=True)
    validated_at = serializers.DateTimeField(allow_null=True)
    capabilities_etag = serializers.CharField(allow_blank=True)
    capabilities_synced_at = serializers.DateTimeField(allow_null=True)
    sync_error_code = serializers.CharField(allow_blank=True)


class OrgIntegrationSerializer(serializers.ModelSerializer):

    """Full serializer for creating / updating org integrations. secret is write-only."""

    secret = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        default="",
        help_text="API token / password. Omit to leave unchanged on PATCH.",
        style={"input_type": "password"},
    )
    has_secret = serializers.SerializerMethodField(
        help_text="True when a secret is stored (value is never returned).",
    )
    integration_type_display = serializers.CharField(source="get_integration_type_display", read_only=True)
    # Present in responses only when integration_type == VPN (removed in to_representation otherwise)
    vpn_secret = OrgIntegrationVPNSecretSerializer(required=False)
    dast_state = serializers.SerializerMethodField()

    # VPN routing FK — nullable, only applicable to non-VPN integration types
    vpn_integration = serializers.PrimaryKeyRelatedField(
        queryset=OrgIntegration.objects.filter(integration_type=OrgIntegrationType.VPN),
        allow_null=True,
        required=False,
        help_text=(
            "Optional VPN integration to route requests through. "
            "Must be a VPN-type integration in the same organization."
        ),
    )

    class Meta:
        model = OrgIntegration
        fields = [
            "id",
            "organization",
            "integration_type",
            "integration_type_display",
            "name",
            "config",
            "secret",
            "has_secret",
            "vpn_secret",
            "vpn_integration",
            "dast_state",
            "is_active",
            "created_by",
            "created",
            "updated",
        ]
        read_only_fields = ["id", "organization", "created_by", "created", "updated"]

    def get_has_secret(self, obj) -> bool:
        return bool(obj.secret)

    @extend_schema_field(DastIntegrationStateSerializer(allow_null=True))
    def get_dast_state(self, obj) -> dict | None:
        state = getattr(obj, "dast_state", None)
        if state is None:
            return None
        return {
            "validation_state": state.validation_state,
            "validation_error_code": state.validation_error_code,
            "contract_version": state.contract_version,
            "validated_at": state.validated_at,
            "capabilities_etag": state.capabilities_etag,
            "capabilities_synced_at": state.capabilities_synced_at,
            "sync_error_code": state.sync_error_code,
        }

    def validate(self, attrs):
        # Application-level guards specific to integration_type. Centralised
        # here so the view stays a thin dispatcher and DB constraints are
        # never exposed as 500s to API clients.
        itype = attrs.get("integration_type") or (
            self.instance.integration_type if self.instance is not None else None
        )
        if itype == OrgIntegrationType.CLAUDE_CODE:
            self._validate_claude_attrs(attrs)
        if itype == OrgIntegrationType.GERRIT:
            self._validate_gerrit_attrs(attrs)
        if itype == OrgIntegrationType.GITEA:
            self._validate_gitea_attrs(attrs)
        if itype == OrgIntegrationType.DAST:
            self._validate_dast_attrs(attrs)
        organization = self.context.get("organization")
        name = attrs.get("name") or (self.instance.name if self.instance is not None else None)
        if organization is not None and itype and name:
            duplicate = OrgIntegration.objects.filter(
                organization=organization,
                integration_type=itype,
                name=name,
            )
            if self.instance is not None:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise serializers.ValidationError({"name": "An integration with this name and type already exists."})
        return super().validate(attrs)

    def _effective_config(self, attrs):
        """
        Merge in-flight ``attrs['config']`` with the existing instance's config on a PATCH
        that doesn't touch config — the single place every per-type validator resolves "the
        config this save will actually end up with" from, instead of each repeating the
        attrs-vs-instance fallback itself.
        """
        config = attrs.get("config")
        if config is None and self.instance is not None:
            config = self.instance.config or {}
        return config or {}

    def _require_config_key(self, attrs, key, message):
        """
        Raise unless ``config[key]`` (post-save, per ``_effective_config``) is a non-empty
        string — the shared shape behind Gerrit's username, Gitea's base_url, and DAST's
        gateway_url requirements.
        """
        if not (self._effective_config(attrs).get(key) or "").strip():
            raise serializers.ValidationError({"config": message})

    def _validate_gerrit_attrs(self, attrs):
        """Gerrit needs an HTTP username (in config) alongside the HTTP password (secret)."""
        self._require_config_key(attrs, "username", "Gerrit integration requires a 'username' in config.")

    def _validate_gitea_attrs(self, attrs):
        """Gitea is always self-hosted — there is no public default like gitlab.com."""
        self._require_config_key(attrs, "base_url", "Gitea integration requires a 'base_url' in config.")

    def _validate_dast_attrs(self, attrs):
        """Validate and canonicalize the complete versioned DAST connection snapshot."""
        try:
            config = DastIntegrationConfig.from_snapshot(self._effective_config(attrs))
        except DastConfigError as exc:
            raise serializers.ValidationError({"config": str(exc)}) from exc
        attrs["config"] = config.to_snapshot()

    def _validate_claude_attrs(self, attrs):
        """
        All CLAUDE_CODE-specific create/update guards in one place.

        Delegates format validation to ``aist/integrations/claude.py`` so
        the OAuth-token regex stays in its single source of truth (I1).
        """
        from aist.integrations.claude import validate_claude_secret_format  # noqa: PLC0415

        secret = attrs.get("secret")
        if secret is None and self.instance is not None:
            # PATCH without secret → leave existing value alone.
            secret = self.instance.secret
        config = attrs.get("config")
        if config is None and self.instance is not None:
            config = self.instance.config or {}
        config = config or {}
        auth_mode = config.get("auth_mode", "oauth")

        ok, detail = validate_claude_secret_format(secret or "", auth_mode=auth_mode)
        if not ok:
            raise serializers.ValidationError({"secret": detail})

        is_active = attrs.get("is_active")
        if is_active is None:
            is_active = self.instance.is_active if self.instance is not None else True
        if is_active:
            organization = self.context.get("organization")
            organization_id = (
                organization.pk if organization is not None
                else (self.instance.organization_id if self.instance is not None else None)
            )
            if organization_id is not None:
                conflicting = OrgIntegration.objects.filter(
                    organization_id=organization_id,
                    integration_type=OrgIntegrationType.CLAUDE_CODE,
                    is_active=True,
                )
                if self.instance is not None:
                    conflicting = conflicting.exclude(pk=self.instance.pk)
                if conflicting.exists():
                    msg = (
                        "Only one active Claude integration is allowed per organization. "
                        "Deactivate the existing one before creating a new active integration."
                    )
                    raise serializers.ValidationError({"is_active": msg})

    def validate_vpn_integration(self, value):
        if value is None:
            return value
        organization = self.context.get("organization")
        organization_id = (
            organization.pk if organization is not None
            else (self.instance.organization_id if self.instance is not None else None)
        )
        # Unconditional check — raise even if organization_id is None/empty so that
        # a missing context never silently allows cross-org VPN linkage.
        if not organization_id:
            msg = "Cannot determine organization context; VPN integration cannot be validated."
            raise serializers.ValidationError(msg)
        if str(value.organization_id) != str(organization_id):
            msg = "VPN integration must belong to the same organization."
            raise serializers.ValidationError(msg)
        return value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Only include vpn_secret for VPN integrations; for all others it's irrelevant noise
        if instance.integration_type != OrgIntegrationType.VPN:
            data.pop("vpn_secret", None)
        # VPN integrations cannot themselves route through another VPN
        if instance.integration_type == OrgIntegrationType.VPN:
            data.pop("vpn_integration", None)
        if instance.integration_type == OrgIntegrationType.DAST:
            data["config"] = instance.get_dast_config().to_snapshot()
        else:
            data.pop("dast_state", None)
        return data

    def update(self, instance, validated_data):
        previous_dast_connection = None
        if instance.integration_type == OrgIntegrationType.DAST:
            previous_dast_connection = (
                instance.config,
                instance.secret,
                instance.vpn_integration_id,
                instance.is_active,
            )
        # On PATCH: if secret is not provided at all, keep existing value.
        if "secret" not in self.initial_data:
            validated_data.pop("secret", None)
        vpn_data = validated_data.pop("vpn_secret", None)
        instance = super().update(instance, validated_data)
        if instance.integration_type == OrgIntegrationType.VPN and vpn_data is not None:
            vpn_secret, _ = OrgIntegrationVPNSecret.objects.get_or_create(integration=instance)
            vpn_ser = OrgIntegrationVPNSecretSerializer(
                vpn_secret,
                data=vpn_data,
                partial=True,
                context=self.context,
            )
            vpn_ser.is_valid(raise_exception=True)
            vpn_ser.save()
            instance.refresh_from_db()
        if instance.integration_type == OrgIntegrationType.DAST:
            current_dast_connection = (
                instance.config,
                instance.secret,
                instance.vpn_integration_id,
                instance.is_active,
            )
            if current_dast_connection != previous_dast_connection:
                schedule_dast_validation(instance)
        elif instance.integration_type == OrgIntegrationType.VPN and (
            vpn_data is not None
            or any(field in self.initial_data for field in ("config", "secret", "is_active"))
        ):
            mark_vpn_linked_dast_validations_pending(instance)
        return instance

    def create(self, validated_data):
        vpn_data = validated_data.pop("vpn_secret", None)
        instance = super().create(validated_data)
        if instance.integration_type == OrgIntegrationType.VPN:
            OrgIntegrationVPNSecret.objects.create(integration=instance, **(vpn_data or {}))
            instance.refresh_from_db()
        elif instance.integration_type == OrgIntegrationType.DAST:
            schedule_dast_validation(instance)
        return instance


class ProjectIntegrationOverrideSerializer(serializers.ModelSerializer):

    """Serializer for per-project integration overrides."""

    class Meta:
        model = ProjectIntegrationOverride
        fields = [
            "id",
            "project",
            "integration_type",
            "org_integration",
            "config_override",
            "is_disabled",
        ]
        read_only_fields = ["id", "project", "integration_type"]

    def validate_org_integration(self, value):
        project = self.context["project"]
        if value is not None:
            if value.organization_id != project.organization_id:
                msg = "Integration belongs to a different organization."
                raise serializers.ValidationError(msg)
            integration_type = self.instance.integration_type if self.instance is not None else None
            if integration_type and value.integration_type != integration_type:
                msg = "Integration type must match the override type."
                raise serializers.ValidationError(msg)
        return value


# ---------------------------------------------------------------------------
# Org-level integration views
# ---------------------------------------------------------------------------


class OrgIntegrationListCreateAPI(AISTAPIView):

    """
    GET  /organizations/<org_id>/integrations/   — list integrations for an org
    POST /organizations/<org_id>/integrations/   — create a new integration
    """

    # Org-scoped: managing an org's integrations is Manage_Members for read and write.
    authz = ResourcePolicy(resource=Organization, read=Action.ORG_MANAGE_READ, write=Action.ORG_MANAGE)

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="List org integrations",
        parameters=[OpenApiParameter("org_id", int, OpenApiParameter.PATH)],
        responses={200: OrgIntegrationSerializer(many=True)},
    )
    def get(self, request, org_id: int):
        org = self.resolve(pk=org_id)
        qs = org.integrations.all()
        return Response(OrgIntegrationSerializer(qs, many=True).data)

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Create org integration",
        parameters=[OpenApiParameter("org_id", int, OpenApiParameter.PATH)],
        request=OrgIntegrationSerializer,
        responses={
            201: OrgIntegrationSerializer,
            400: OpenApiResponse(description="Validation error"),
            403: OpenApiResponse(description="Forbidden"),
            409: OpenApiResponse(description="An active DAST integration already exists"),
        },
    )
    def post(self, request, org_id: int):
        org = self.resolve(pk=org_id)
        serializer = OrgIntegrationSerializer(data=request.data, context={"organization": org})
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                serializer.save(organization=org, created_by=request.user)
        except IntegrityError as exc:
            if "one_active_dast_integration_per_org" not in str(exc):
                raise
            return Response(
                {"detail": "Only one active DAST integration is allowed per organization."},
                status=status.HTTP_409_CONFLICT,
            )
        if serializer.instance.integration_type == OrgIntegrationType.DAST:
            audit_event(
                "dast_integration_imported",
                context=AuditContext(
                    organization_id=org.pk,
                    integration_id=serializer.instance.pk,
                    actor_id=request.user.pk,
                ),
            )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class OrgIntegrationDetailAPI(AISTAPIView):

    """
    GET    /integrations/<integration_id>/
    PATCH  /integrations/<integration_id>/
    DELETE /integrations/<integration_id>/
    """

    # Each write here can store a new DAST connection, and storing one schedules a probe of the
    # tenant-supplied gateway URL. Bound how often one actor can drive that; reads stay free.
    throttle_scope = "aist_dast_gateway_probe"

    def get_throttles(self):
        if self.request.method in SAFE_METHODS:
            return []
        return [ScopedRateThrottle()]

    authz = ResourcePolicy(resource=OrgIntegration, read=Action.ORG_MANAGE_READ, write=Action.ORG_MANAGE)

    def _get_integration(self, integration_id: int) -> OrgIntegration:
        return self.resolve(pk=integration_id)

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Retrieve an org integration",
        responses={200: OrgIntegrationSerializer},
    )
    def get(self, request, integration_id: int):
        return Response(OrgIntegrationSerializer(self._get_integration(integration_id)).data)

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Update an org integration",
        request=OrgIntegrationSerializer,
        responses={
            200: OrgIntegrationSerializer,
            400: OpenApiResponse(description="Validation error"),
            409: OpenApiResponse(description="An active DAST integration already exists"),
        },
    )
    def patch(self, request, integration_id: int):
        integration = self._get_integration(integration_id)
        serializer = OrgIntegrationSerializer(
            integration,
            data=request.data,
            partial=True,
            context={"organization": integration.organization},
        )
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                serializer.save()
        except IntegrityError as exc:
            if "one_active_dast_integration_per_org" not in str(exc):
                raise
            return Response(
                {"detail": "Only one active DAST integration is allowed per organization."},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(serializer.data)

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Delete an org integration",
        responses={204: None},
    )
    def delete(self, request, integration_id: int):
        self._get_integration(integration_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class OrganizationDastIntegrationImportAPI(AISTAPIView):

    """Import the one active DAST integration for an authorized organization."""

    # Each write here can store a new DAST connection, and storing one schedules a probe of the
    # tenant-supplied gateway URL. Bound how often one actor can drive that; reads stay free.
    throttle_scope = "aist_dast_gateway_probe"

    def get_throttles(self):
        if self.request.method in SAFE_METHODS:
            return []
        return [ScopedRateThrottle()]

    authz = ResourcePolicy(resource=Organization, read=Action.ORG_MANAGE_READ, write=Action.ORG_MANAGE)
    serializer_class = DastIntegrationOnboardingSerializer

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Retrieve the active DAST integration",
        responses={200: OrgIntegrationSerializer},
    )
    def get(self, request, org_id: int):
        organization = self.resolve(pk=org_id)
        integration = OrgIntegration.objects.filter(
            organization=organization,
            integration_type=OrgIntegrationType.DAST,
            is_active=True,
        ).first()
        if integration is None:
            raise Http404
        return Response(OrgIntegrationSerializer(integration).data)

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Import a versioned DAST onboarding bundle",
        request=DastIntegrationOnboardingSerializer,
        responses={
            201: OrgIntegrationSerializer,
            400: OpenApiResponse(description="Invalid onboarding bundle"),
            409: OpenApiResponse(
                description="An active DAST integration already exists, or this bundle was already imported",
            ),
        },
    )
    def post(self, request, org_id: int):
        organization = self.resolve(pk=org_id)
        serializer = DastIntegrationOnboardingSerializer(
            data=request.data,
            context={"organization": organization, "requester": request.user},
        )
        serializer.is_valid(raise_exception=True)
        integrator_public_id = serializer.validated_data["bundle"]["integrator_public_id"]
        try:
            with transaction.atomic():
                serializer.save()
                # Claiming the bundle inside the same transaction as the integration it
                # creates: a UNIQUE constraint on integrator_public_id makes re-importing
                # the same exported bundle (elsewhere, or after this integration is later
                # deleted) fail closed instead of silently succeeding again.
                DastOnboardingBundleUse.objects.create(
                    integrator_public_id=integrator_public_id,
                    org_integration=serializer.instance,
                )
        except IntegrityError as exc:
            message = str(exc)
            if "dastonboardingbundleuse" in message.lower():
                return Response(
                    {"detail": "This onboarding bundle has already been used."},
                    status=status.HTTP_409_CONFLICT,
                )
            if "one_active_dast_integration_per_org" not in message:
                raise
            return Response(
                {"detail": "Only one active DAST integration is allowed per organization."},
                status=status.HTTP_409_CONFLICT,
            )
        audit_event(
            "dast_integration_imported",
            context=AuditContext(
                organization_id=organization.pk,
                integration_id=serializer.instance.pk,
                actor_id=request.user.pk,
            ),
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class DastIntegrationOnboardingDetailAPI(AISTAPIView):

    """Read or replace a DAST connection using the strict bundle boundary."""

    # Each write here can store a new DAST connection, and storing one schedules a probe of the
    # tenant-supplied gateway URL. Bound how often one actor can drive that; reads stay free.
    throttle_scope = "aist_dast_gateway_probe"

    def get_throttles(self):
        if self.request.method in SAFE_METHODS:
            return []
        return [ScopedRateThrottle()]

    authz = ResourcePolicy(resource=OrgIntegration, read=Action.ORG_MANAGE_READ, write=Action.ORG_MANAGE)
    serializer_class = DastIntegrationOnboardingSerializer

    def _integration(self, integration_id: int):
        integration = self.resolve(pk=integration_id)
        if integration.integration_type != OrgIntegrationType.DAST:
            raise Http404
        return integration

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Retrieve a DAST onboarding connection",
        responses={200: OrgIntegrationSerializer},
    )
    def get(self, request, integration_id: int):
        return Response(OrgIntegrationSerializer(self._integration(integration_id)).data)

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Replace a DAST onboarding bundle",
        request=DastIntegrationOnboardingSerializer,
        responses={200: OrgIntegrationSerializer, 400: OpenApiResponse(description="Invalid onboarding bundle")},
    )
    def patch(self, request, integration_id: int):
        integration = self._integration(integration_id)
        serializer = DastIntegrationOnboardingSerializer(
            integration,
            data=request.data,
            partial=True,
            context={"organization": integration.organization, "requester": request.user},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        audit_event(
            "dast_integration_updated",
            context=AuditContext(
                organization_id=integration.organization_id,
                integration_id=integration.pk,
                actor_id=request.user.pk,
            ),
        )
        return Response(serializer.data)


class DastIntegrationDisableAPI(AISTAPIView):

    authz = ResourcePolicy(resource=OrgIntegration, read=Action.ORG_MANAGE_READ, write=Action.ORG_MANAGE)
    serializer_class = OrgIntegrationSerializer

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Disable a DAST integration",
        request=None,
        responses={200: OrgIntegrationSerializer},
    )
    def post(self, request, integration_id: int):
        integration = self.resolve(pk=integration_id)
        if integration.integration_type != OrgIntegrationType.DAST:
            raise Http404
        integration.is_active = False
        integration.save(update_fields=["is_active", "updated"])
        mark_dast_validation_pending(integration)
        audit_event(
            "dast_integration_disabled",
            context=AuditContext(
                organization_id=integration.organization_id,
                integration_id=integration.pk,
                actor_id=request.user.pk,
            ),
        )
        return Response(OrgIntegrationSerializer(integration).data)


class DastIntegrationTokenRotateAPI(AISTAPIView):

    # Each write here can store a new DAST connection, and storing one schedules a probe of the
    # tenant-supplied gateway URL. Bound how often one actor can drive that; reads stay free.
    throttle_scope = "aist_dast_gateway_probe"

    def get_throttles(self):
        if self.request.method in SAFE_METHODS:
            return []
        return [ScopedRateThrottle()]

    authz = ResourcePolicy(resource=OrgIntegration, read=Action.ORG_MANAGE_READ, write=Action.ORG_MANAGE)

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Store a newly rotated DAST integrator token",
        request=DastTokenRotationSerializer,
        responses={200: OrgIntegrationSerializer, 400: OpenApiResponse(description="Invalid token")},
    )
    def post(self, request, integration_id: int):
        integration = self.resolve(pk=integration_id)
        if integration.integration_type != OrgIntegrationType.DAST:
            raise Http404
        serializer = DastTokenRotationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        integration.secret = serializer.validated_data["token"]
        integration.save(update_fields=["secret", "updated"])
        schedule_dast_validation(integration)
        audit_event(
            "dast_token_rotated",
            context=AuditContext(
                organization_id=integration.organization_id,
                integration_id=integration.pk,
                actor_id=request.user.pk,
            ),
        )
        return Response(OrgIntegrationSerializer(integration).data)


class OrgIntegrationValidateAPI(AISTAPIView):

    """
    POST /integrations/<integration_id>/validate/

    Dispatches credential validation to a Celery worker (which has Docker socket
    access for VPN-routed checks).  Returns 202 {"task_id": "..."} immediately.
    Poll GET /integrations/<id>/validate/<task_id>/ for the result.
    """

    # Each write here can store a new DAST connection, and storing one schedules a probe of the
    # tenant-supplied gateway URL. Bound how often one actor can drive that; reads stay free.
    throttle_scope = "aist_dast_gateway_probe"

    def get_throttles(self):
        if self.request.method in SAFE_METHODS:
            return []
        return [ScopedRateThrottle()]

    authz = ResourcePolicy(resource=OrgIntegration, read=Action.ORG_MANAGE_READ, write=Action.ORG_MANAGE)

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Start async integration credential validation",
        request=None,
        responses={
            202: {"type": "object", "properties": {"task_id": {"type": "string"}}},
        },
    )
    def post(self, request, integration_id: int):
        integration = self.resolve(pk=integration_id)
        if integration.integration_type == OrgIntegrationType.DAST:
            ticket = schedule_dast_validation(integration)
            return Response({"task_id": ticket.task_id}, status=status.HTTP_202_ACCEPTED)
        from aist.tasks.validate import validate_integration  # noqa: PLC0415

        result = validate_integration.delay(integration.pk)
        return Response({"task_id": result.id}, status=status.HTTP_202_ACCEPTED)


class OrgIntegrationValidateStatusAPI(AISTAPIView):

    """
    GET /integrations/<integration_id>/validate/<task_id>/

    Returns the result of a previously dispatched validation task.
    State is one of PENDING, STARTED, SUCCESS, FAILURE.
    """

    authz = ResourcePolicy(resource=OrgIntegration, read=Action.ORG_MANAGE_READ, write=Action.ORG_MANAGE)

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Poll validation task status",
        parameters=[
            OpenApiParameter("integration_id", int, OpenApiParameter.PATH),
            OpenApiParameter("task_id", str, OpenApiParameter.PATH),
        ],
        responses={
            200: {
                "type": "object",
                "properties": {
                    "state": {"type": "string"},
                    "valid": {"type": "boolean", "nullable": True},
                    "detail": {"type": "string"},
                },
            },
        },
    )
    def get(self, request, integration_id: int, task_id: str):
        integration = self.resolve(pk=integration_id)
        if integration.integration_type == OrgIntegrationType.DAST:
            dast_state = getattr(integration, "dast_state", None)
            if dast_state is None or dast_state.validation_task_id != task_id:
                return Response({"state": "PENDING", "valid": None, "detail": ""})
            return Response({
                "state": dast_state.validation_state,
                "valid": (
                    True
                    if dast_state.validation_state == "READY"
                    else False if dast_state.validation_state == "INVALID" else None
                ),
                "detail": dast_state.validation_error_code,
            })
        from celery.result import AsyncResult  # noqa: PLC0415

        ar = AsyncResult(task_id)
        if ar.state == "SUCCESS":
            result = ar.result
            # Verify this task was dispatched for the requested integration
            # (prevents reading another integration's result via a guessed task_id).
            if result.get("_integration_id") != integration_id:
                return Response({"state": "PENDING", "valid": None, "detail": ""})
            return Response({"state": "SUCCESS", "valid": result["valid"], "detail": result["detail"]})
        if ar.state == "FAILURE":
            # Verify task ownership before returning any error details to the caller.
            # ar.result for FAILURE is the meta dict set in validate_integration task.
            meta = ar.result if isinstance(ar.result, dict) else {}
            if meta.get("_integration_id") != integration_id:
                return Response({"state": "PENDING", "valid": None, "detail": ""})
            return Response({"state": "FAILURE", "valid": False, "detail": "Validation failed — see server logs."})
        return Response({"state": ar.state, "valid": None, "detail": ""})


def _validate_integration(integration: OrgIntegration) -> tuple[bool, str]:
    """Return (valid, detail) without raising on credential failures."""
    itype = integration.integration_type
    secret = integration.secret or ""
    config = integration.config or {}

    if itype == OrgIntegrationType.GITLAB:
        import gitlab  # noqa: PLC0415

        base_url = config.get("base_url") or "https://gitlab.com"
        try:
            with integration.scoped_session(execution_id=f"validate-{integration.pk}") as session:
                gl = gitlab.Gitlab(base_url, private_token=secret or None, session=session)
                gl.auth()
        except Exception as exc:
            logger.exception("Integration[%s] GITLAB validation error", integration.pk)
            return False, f"Validation failed ({type(exc).__name__}) — see server logs."
        else:
            return True, ""

    if itype == OrgIntegrationType.SLACK:
        from aist.notifications import AISTSlackNotificationManager  # noqa: PLC0415

        try:
            mgr = AISTSlackNotificationManager()
            ok = mgr.test_token(secret)
        except Exception as exc:
            logger.exception("Integration[%s] SLACK validation error", integration.pk)
            return False, f"Validation failed ({type(exc).__name__}) — see server logs."
        else:
            return ok, "" if ok else "Slack auth failed"

    if itype == OrgIntegrationType.GITHUB:
        # GitHub App auth is validated at installation time; no stored secret to test.
        return True, "GitHub uses App-level auth; no credential stored."

    if itype == OrgIntegrationType.GERRIT:
        from aist.tasks.integrations import _gerrit_rest  # noqa: PLC0415

        if not (config.get("base_url") or "").strip():
            return False, "Gerrit integration requires a base_url in config."
        try:
            with integration.scoped_session(execution_id=f"validate-{integration.pk}") as session:
                rest, _base_url = _gerrit_rest(integration, session)
                rest.get("/accounts/self")
        except Exception as exc:
            logger.exception("Integration[%s] GERRIT validation error", integration.pk)
            return False, f"Validation failed ({type(exc).__name__}) — see server logs."
        else:
            return True, ""

    if itype == OrgIntegrationType.GITEA:
        from aist.tasks.integrations import _gitea_headers  # noqa: PLC0415

        base_url = (config.get("base_url") or "").rstrip("/")
        if not base_url:
            return False, "Gitea integration requires a base_url in config."
        try:
            with integration.scoped_session(execution_id=f"validate-{integration.pk}") as session:
                # /api/v1/user requires the "read:user" scope, which a token scoped only
                # for repository access (the actual use of this integration) won't have.
                # /api/v1/repos/search only needs "read:repository", matching what
                # fetch_gitea_projects actually calls.
                resp = session.get(
                    f"{base_url}/api/v1/repos/search",
                    headers=_gitea_headers(integration),
                    params={"limit": 1},
                    timeout=15,
                )
                resp.raise_for_status()
        except Exception as exc:
            logger.exception("Integration[%s] GITEA validation error", integration.pk)
            return False, f"Validation failed ({type(exc).__name__}) — see server logs."
        else:
            return True, ""

    if itype == OrgIntegrationType.EMAIL:
        # Basic SMTP connectivity check would require opening a socket; skip for now.
        return True, "Email configuration is not automatically validated."

    if itype == OrgIntegrationType.VPN:
        return _validate_vpn_integration(integration)

    if itype == OrgIntegrationType.CLAUDE_CODE:
        # All Claude-specific knowledge lives in aist/integrations/claude.py
        # (architectural invariant I1). Dispatch is a one-liner.
        from aist.integrations.claude import probe_claude_token  # noqa: PLC0415
        return probe_claude_token(integration)

    if itype == OrgIntegrationType.DAST:
        # All DAST-specific knowledge lives in aist/integrations/dast.py, mirroring
        # the Claude single-concentrator invariant above. Dispatch is a one-liner.
        from aist.integrations.dast import probe_dast_gateway  # noqa: PLC0415
        return probe_dast_gateway(integration)

    return False, f"No validator for integration type {itype}"


def _parse_remote_from_ovpn(ovpn_content: str) -> str | None:
    """Return the hostname from the first 'remote <host> [port] [proto]' line."""
    for line in ovpn_content.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] == "remote":
            return parts[1]
    return None


def _validate_vpn_integration(integration: OrgIntegration) -> tuple[bool, str]:
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


# ---------------------------------------------------------------------------
# Project-level override views
# ---------------------------------------------------------------------------


class ProjectIntegrationOverrideAPI(AISTAPIView):

    """
    GET    /projects/<project_id>/integration-overrides/
    PUT    /projects/<project_id>/integration-overrides/<type>/   (upsert)
    DELETE /projects/<project_id>/integration-overrides/<type>/
    """

    # G-2: mutating a project's integration override is a project config change
    # (Product_Edit / Maintainer+); reads stay Product_View.
    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    def _get_project(self, project_id: int):
        return self.resolve(pk=project_id)

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="List integration overrides for a project",
        parameters=[OpenApiParameter("project_id", int, OpenApiParameter.PATH)],
        responses={200: ProjectIntegrationOverrideSerializer(many=True)},
    )
    def get(self, request, project_id: int):
        project = self._get_project(project_id)
        overrides = ProjectIntegrationOverride.objects.filter(project=project).select_related("org_integration")
        return Response(ProjectIntegrationOverrideSerializer(overrides, many=True).data)


class ProjectIntegrationOverrideDetailAPI(AISTAPIView):

    """
    PUT    /projects/<project_id>/integration-overrides/<integration_type>/
    DELETE /projects/<project_id>/integration-overrides/<integration_type>/
    """

    # G-2: mutating a project's integration override is a project config change
    # (Product_Edit / Maintainer+); reads stay Product_View.
    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    _VALID_TYPES = {t.value for t in OrgIntegrationType}

    def _get_project(self, project_id: int):
        return self.resolve(pk=project_id)

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Upsert project integration override",
        parameters=[
            OpenApiParameter("project_id", int, OpenApiParameter.PATH),
            OpenApiParameter("integration_type", str, OpenApiParameter.PATH),
        ],
        request=ProjectIntegrationOverrideSerializer,
        responses={200: ProjectIntegrationOverrideSerializer, 400: OpenApiResponse(description="Validation error")},
    )
    def put(self, request, project_id: int, integration_type: str):
        if integration_type not in self._VALID_TYPES:
            return Response(
                {"integration_type": f"Must be one of {sorted(self._VALID_TYPES)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        project = self._get_project(project_id)
        override, _ = ProjectIntegrationOverride.objects.get_or_create(
            project=project,
            integration_type=integration_type,
        )
        serializer = ProjectIntegrationOverrideSerializer(
            override,
            data=request.data,
            partial=True,
            context={"project": project},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(ProjectIntegrationOverrideSerializer(override).data)

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Delete project integration override",
        parameters=[
            OpenApiParameter("project_id", int, OpenApiParameter.PATH),
            OpenApiParameter("integration_type", str, OpenApiParameter.PATH),
        ],
        responses={204: None},
    )
    def delete(self, request, project_id: int, integration_type: str):
        project = self._get_project(project_id)
        deleted, _ = ProjectIntegrationOverride.objects.filter(
            project=project,
            integration_type=integration_type,
        ).delete()
        if not deleted:
            raise Http404
        return Response(status=status.HTTP_204_NO_CONTENT)
