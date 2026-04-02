from __future__ import annotations

from django.shortcuts import get_object_or_404
from dojo.authorization.roles_permissions import Permissions
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.query import AuthorizedQuerySetMixin, AuthorizedQuerysetSpec
from aist.api.schema import AISTApiTag
from aist.models import OrgIntegration, OrgIntegrationVPNSecret, OrgIntegrationType, ProjectIntegrationOverride
from aist.queries import (
    get_authorized_aist_organizations,
    get_authorized_aist_projects,
    get_authorized_org_integrations,
)

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
        write_only=True, required=False, allow_blank=True,
        style={"input_type": "password"},
        help_text="Full .ovpn file content (inline <ca>/<cert>/<key> blocks supported).",
    )
    ca_cert = serializers.CharField(write_only=True, required=False, allow_blank=True)
    client_cert = serializers.CharField(write_only=True, required=False, allow_blank=True)
    client_key = serializers.CharField(
        write_only=True, required=False, allow_blank=True,
        style={"input_type": "password"},
    )
    tls_auth_key = serializers.CharField(
        write_only=True, required=False, allow_blank=True,
        style={"input_type": "password"},
    )
    vpn_username = serializers.CharField(write_only=True, required=False, allow_blank=True)
    vpn_password = serializers.CharField(
        write_only=True, required=False, allow_blank=True,
        style={"input_type": "password"},
    )
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

    def update(self, instance, validated_data):
        # On PATCH: preserve fields not explicitly provided in the request body.
        secret_fields = (
            "ovpn_content", "ca_cert", "client_cert", "client_key",
            "tls_auth_key", "vpn_username", "vpn_password",
        )
        for field in secret_fields:
            if field not in self.initial_data:
                validated_data.pop(field, None)
        return super().update(instance, validated_data)

    class Meta:
        model = OrgIntegrationVPNSecret
        fields = [
            "ovpn_content", "ca_cert", "client_cert", "client_key", "tls_auth_key",
            "vpn_username", "vpn_password",
            "has_ovpn_content", "has_client_cert", "has_client_key", "has_username",
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
            "is_active",
            "created_by",
            "created",
            "updated",
        ]
        read_only_fields = ["id", "created_by", "created", "updated"]

    def get_has_secret(self, obj) -> bool:
        return bool(obj.secret)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Only include vpn_secret for VPN integrations; for all others it's irrelevant noise
        if instance.integration_type != OrgIntegrationType.VPN:
            data.pop("vpn_secret", None)
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
                vpn_secret, data=vpn_data, partial=True, context=self.context,
            )
            vpn_ser.is_valid(raise_exception=True)
            vpn_ser.save()
        return instance

    def create(self, validated_data):
        vpn_data = validated_data.pop("vpn_secret", None)
        instance = super().create(validated_data)
        if instance.integration_type == OrgIntegrationType.VPN:
            OrgIntegrationVPNSecret.objects.create(integration=instance, **(vpn_data or {}))
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

    Tests connectivity / credentials for the integration.
    Returns 200 {"valid": true/false} — never raises 5xx for credential failures.
    """

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_org_integrations,
        permission=Permissions.Product_Type_Manage_Members,
    )

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Validate integration credentials",
        request=None,
        responses={
            200: {"type": "object", "properties": {"valid": {"type": "boolean"}, "detail": {"type": "string"}}},
        },
    )
    def post(self, request, integration_id: int):
        integration = self.get_authorized_object(pk=integration_id)
        valid, detail = _validate_integration(integration)
        return Response({"valid": valid, "detail": detail})


def _validate_integration(integration: OrgIntegration) -> tuple[bool, str]:
    """Return (valid, detail) without raising on credential failures."""
    itype = integration.integration_type
    secret = integration.secret or ""
    config = integration.config or {}

    if itype == OrgIntegrationType.GITLAB:
        import gitlab  # noqa: PLC0415

        base_url = config.get("base_url") or "https://gitlab.com"
        try:
            gl = gitlab.Gitlab(base_url, private_token=secret or None)
            gl.auth()
        except Exception as exc:
            return False, str(exc)
        else:
            return True, ""

    if itype == OrgIntegrationType.SLACK:
        from aist.notifications import AISTSlackNotificationManager  # noqa: PLC0415

        try:
            mgr = AISTSlackNotificationManager()
            ok = mgr.test_token(secret)
        except Exception as exc:
            return False, str(exc)
        else:
            return ok, "" if ok else "Slack auth failed"

    if itype == OrgIntegrationType.GITHUB:
        # GitHub App auth is validated at installation time; no stored secret to test.
        return True, "GitHub uses App-level auth; no credential stored."

    if itype == OrgIntegrationType.EMAIL:
        # Basic SMTP connectivity check would require opening a socket; skip for now.
        return True, "Email configuration is not automatically validated."

    if itype == OrgIntegrationType.VPN:
        return _validate_vpn_integration(integration)

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
    import subprocess  # noqa: PLC0415

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
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "5", ping_target],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, f"{ping_target} is reachable."
        return False, f"{ping_target} unreachable (ping returned {result.returncode})."
    except subprocess.TimeoutExpired:
        return False, f"Ping to {ping_target} timed out."
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


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
