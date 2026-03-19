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
from aist.models import OrgIntegration, OrgIntegrationType, ProjectIntegrationOverride
from aist.queries import (
    get_authorized_aist_organizations,
    get_authorized_aist_projects,
    get_authorized_org_integrations,
)

# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


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
            "is_active",
            "created_by",
            "created",
            "updated",
        ]
        read_only_fields = ["id", "created_by", "created", "updated"]

    def get_has_secret(self, obj) -> bool:
        return bool(obj.secret)

    def update(self, instance, validated_data):
        # On PATCH: if secret is not provided at all, keep existing value.
        if "secret" not in self.initial_data:
            validated_data.pop("secret", None)
        return super().update(instance, validated_data)


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
        permission=Permissions.Product_View,
    )

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="List org integrations",
        parameters=[OpenApiParameter("org_id", int, OpenApiParameter.PATH)],
        responses={200: OrgIntegrationSerializer(many=True)},
    )
    def get(self, request, org_id: int):
        qs = self.get_authorized_queryset().filter(organization_id=org_id)
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
            get_authorized_aist_organizations(Permissions.Product_View, user=request.user),
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
        permission=Permissions.Product_View,
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
        permission=Permissions.Product_View,
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

    return False, f"No validator for integration type {itype}"


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
