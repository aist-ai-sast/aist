from __future__ import annotations

import logging
import re
import shutil
import subprocess

from django.shortcuts import get_object_or_404
from dojo.authorization.roles_permissions import Permissions
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.query import AuthorizedQuerySetMixin, AuthorizedQuerysetSpec
from aist.api.schema import AISTApiTag
from aist.models import OrgIntegration, OrgIntegrationType, OrgIntegrationVPNSecret, ProjectIntegrationOverride
from aist.queries import (
    get_authorized_aist_organizations,
    get_authorized_aist_projects,
    get_authorized_org_integrations,
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
            "is_active",
            "created_by",
            "created",
            "updated",
        ]
        read_only_fields = ["id", "created_by", "created", "updated"]

    def get_has_secret(self, obj) -> bool:
        return bool(obj.secret)

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
        return super().validate(attrs)

    def _validate_gerrit_attrs(self, attrs):
        """Gerrit needs an HTTP username (in config) alongside the HTTP password (secret)."""
        config = attrs.get("config")
        if config is None and self.instance is not None:
            config = self.instance.config or {}
        config = config or {}
        if not (config.get("username") or "").strip():
            raise serializers.ValidationError(
                {"config": "Gerrit integration requires a 'username' in config."},
            )

    def _validate_gitea_attrs(self, attrs):
        """Gitea is always self-hosted — there is no public default like gitlab.com."""
        config = attrs.get("config")
        if config is None and self.instance is not None:
            config = self.instance.config or {}
        config = config or {}
        if not (config.get("base_url") or "").strip():
            raise serializers.ValidationError(
                {"config": "Gitea integration requires a 'base_url' in config."},
            )

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
            organization_id = (
                attrs.get("organization").pk if attrs.get("organization") is not None
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
        instance = self.instance
        organization_id = instance.organization_id if instance is not None else self.initial_data.get("organization")
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
        return data

    def update(self, instance, validated_data):
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
        return instance

    def create(self, validated_data):
        vpn_data = validated_data.pop("vpn_secret", None)
        instance = super().create(validated_data)
        if instance.integration_type == OrgIntegrationType.VPN:
            OrgIntegrationVPNSecret.objects.create(integration=instance, **(vpn_data or {}))
            instance.refresh_from_db()
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
        read_only_fields = ["id", "project"]


# ---------------------------------------------------------------------------
# Org-level integration views
# ---------------------------------------------------------------------------


class OrgIntegrationListCreateAPI(AuthorizedQuerySetMixin, APIView):

    """
    GET  /organizations/<org_id>/integrations/   — list integrations for an org
    POST /organizations/<org_id>/integrations/   — create a new integration
    """

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_org_integrations,
        permission=Permissions.Product_Type_Manage_Members,
    )

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="List org integrations",
        parameters=[OpenApiParameter("org_id", int, OpenApiParameter.PATH)],
        responses={200: OrgIntegrationSerializer(many=True)},
    )
    def get(self, request, org_id: int):
        org = get_object_or_404(
            get_authorized_aist_organizations(Permissions.Product_Type_Manage_Members, user=request.user),
            pk=org_id,
        )
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
        },
    )
    def post(self, request, org_id: int):
        org = get_object_or_404(
            get_authorized_aist_organizations(Permissions.Product_Type_Manage_Members, user=request.user),
            pk=org_id,
        )
        data = {**request.data, "organization": org.pk}
        serializer = OrgIntegrationSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        serializer.save(created_by=request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class OrgIntegrationDetailAPI(AuthorizedQuerySetMixin, APIView):

    """
    GET    /integrations/<integration_id>/
    PATCH  /integrations/<integration_id>/
    DELETE /integrations/<integration_id>/
    """

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_org_integrations,
        permission=Permissions.Product_Type_Manage_Members,
    )

    def _get_integration(self, integration_id: int) -> OrgIntegration:
        return self.get_authorized_object(pk=integration_id)

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
        responses={200: OrgIntegrationSerializer, 400: OpenApiResponse(description="Validation error")},
    )
    def patch(self, request, integration_id: int):
        integration = self._get_integration(integration_id)
        serializer = OrgIntegrationSerializer(integration, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Delete an org integration",
        responses={204: None},
    )
    def delete(self, request, integration_id: int):
        self._get_integration(integration_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class OrgIntegrationValidateAPI(AuthorizedQuerySetMixin, APIView):

    """
    POST /integrations/<integration_id>/validate/

    Dispatches credential validation to a Celery worker (which has Docker socket
    access for VPN-routed checks).  Returns 202 {"task_id": "..."} immediately.
    Poll GET /integrations/<id>/validate/<task_id>/ for the result.
    """

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_org_integrations,
        permission=Permissions.Product_Type_Manage_Members,
    )

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Start async integration credential validation",
        request=None,
        responses={
            202: {"type": "object", "properties": {"task_id": {"type": "string"}}},
        },
    )
    def post(self, request, integration_id: int):
        integration = self.get_authorized_object(pk=integration_id)
        from aist.tasks.validate import validate_integration  # noqa: PLC0415

        result = validate_integration.delay(integration.pk)
        return Response({"task_id": result.id}, status=status.HTTP_202_ACCEPTED)


class OrgIntegrationValidateStatusAPI(AuthorizedQuerySetMixin, APIView):

    """
    GET /integrations/<integration_id>/validate/<task_id>/

    Returns the result of a previously dispatched validation task.
    State is one of PENDING, STARTED, SUCCESS, FAILURE.
    """

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_org_integrations,
        permission=Permissions.Product_Type_Manage_Members,
    )

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
        self.get_authorized_object(pk=integration_id)  # org isolation check
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


class ProjectIntegrationOverrideAPI(AuthorizedQuerySetMixin, APIView):

    """
    GET    /projects/<project_id>/integration-overrides/
    PUT    /projects/<project_id>/integration-overrides/<type>/   (upsert)
    DELETE /projects/<project_id>/integration-overrides/<type>/
    """

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_projects,
        permission=Permissions.Product_View,
    )

    def _get_project(self, project_id: int):
        return self.get_authorized_object(pk=project_id)

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


class ProjectIntegrationOverrideDetailAPI(AuthorizedQuerySetMixin, APIView):

    """
    PUT    /projects/<project_id>/integration-overrides/<integration_type>/
    DELETE /projects/<project_id>/integration-overrides/<integration_type>/
    """

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_projects,
        permission=Permissions.Product_View,
    )

    _VALID_TYPES = {t.value for t in OrgIntegrationType}

    def _get_project(self, project_id: int):
        return self.get_authorized_object(pk=project_id)

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
        # Cross-org guard: an explicit org_integration must belong to the project's org.
        # This prevents a user from routing a project's integrations through another org's credentials.
        org_integration_id = request.data.get("org_integration")
        if org_integration_id:
            try:
                oi = OrgIntegration.objects.get(pk=org_integration_id)
            except OrgIntegration.DoesNotExist:
                return Response(
                    {"org_integration": "Integration not found."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if oi.organization_id != project.organization_id:
                return Response(
                    {"org_integration": "Integration belongs to a different organization."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        override, _ = ProjectIntegrationOverride.objects.get_or_create(
            project=project,
            integration_type=integration_type,
        )
        serializer = ProjectIntegrationOverrideSerializer(override, data=request.data, partial=True)
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
        get_object_or_404(ProjectIntegrationOverride, project=project, integration_type=integration_type).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
