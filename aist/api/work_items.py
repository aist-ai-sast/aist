from __future__ import annotations

import logging
import uuid
from urllib.parse import urlparse

from celery.result import AsyncResult
from django.shortcuts import get_object_or_404
from dojo.authorization.roles_permissions import Permissions
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.query import AuthorizedQuerySetMixin, AuthorizedQuerysetSpec
from aist.api.schema import AISTApiTag
from aist.models import (
    OrgIntegration,
    OrgIntegrationType,
    WorkItemLink,
    WorkItemProvider,
    WorkItemProviderType,
    WorkItemStatusCategory,
)
from aist.queries import get_authorized_aist_organizations, get_authorized_findings, get_authorized_work_item_providers
from aist.tasks.work_items import sync_work_item_link, sync_work_item_provider
from aist.work_items.backends import get_backend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


class WorkItemProviderSerializer(serializers.ModelSerializer):

    """Full serializer for creating / updating providers. api_token is write-only."""

    api_token = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        default="",
        help_text="API token / personal access token. Omit to leave unchanged on PATCH.",
        style={"input_type": "password"},
    )
    provider_type_display = serializers.CharField(source="get_provider_type_display", read_only=True)
    has_token = serializers.SerializerMethodField(
        help_text="True when an API token is stored (token value is never returned).",
    )

    # VPN integration FK — nullable, must belong to the same organization
    vpn_integration = serializers.PrimaryKeyRelatedField(
        queryset=OrgIntegration.objects.filter(integration_type=OrgIntegrationType.VPN),
        allow_null=True,
        required=False,
        help_text="Optional VPN OrgIntegration (type=VPN) required to reach this provider. Must belong to the same organization.",
    )

    class Meta:
        model = WorkItemProvider
        fields = [
            "id",
            "organization",
            "provider_type",
            "provider_type_display",
            "name",
            "base_url",
            "api_token",
            "has_token",
            "provider_config",
            "sync_enabled",
            "is_active",
            "vpn_integration",
            "created",
            "updated",
        ]
        read_only_fields = ["id", "created", "updated"]

    def get_has_token(self, obj) -> bool:
        return bool(obj.api_token)

    def update(self, instance, validated_data):
        # On PATCH: if api_token is not provided at all, keep existing value
        if "api_token" not in self.initial_data:
            validated_data.pop("api_token", None)
        return super().update(instance, validated_data)

    def validate_vpn_integration(self, value):
        if value is None:
            return value
        # Determine the organization from the instance (PATCH) or the request data (POST)
        instance = self.instance
        organization_id = instance.organization_id if instance is not None else self.initial_data.get("organization")
        if organization_id and str(value.organization_id) != str(organization_id):
            msg = "VPN integration must belong to the same organization as this provider."
            raise serializers.ValidationError(msg)
        return value


class WorkItemLinkSerializer(serializers.ModelSerializer):

    """Serializer for creating / reading WorkItemLinks."""

    status_category_display = serializers.CharField(source="get_status_category_display", read_only=True)
    provider_name = serializers.SerializerMethodField()
    provider_type = serializers.SerializerMethodField()

    class Meta:
        model = WorkItemLink
        fields = [
            "id",
            "finding",
            "provider",
            "provider_name",
            "provider_type",
            "external_id",
            "external_key",
            "external_url",
            "title",
            "raw_status",
            "status_category",
            "status_category_display",
            "last_synced_at",
            "sync_error",
            "created_by",
            "created",
            "updated",
        ]
        read_only_fields = [
            "id",
            "last_synced_at",
            "sync_error",
            "created_by",
            "created",
            "updated",
        ]

    def get_provider_name(self, obj) -> str | None:
        return obj.provider.name if obj.provider_id else None

    def get_provider_type(self, obj) -> str | None:
        return obj.provider.provider_type if obj.provider_id else None


class WorkItemLinkInlineSerializer(serializers.ModelSerializer):

    """Lightweight read-only serializer embedded in Finding list responses."""

    provider_name = serializers.SerializerMethodField()
    provider_type = serializers.SerializerMethodField()

    class Meta:
        model = WorkItemLink
        fields = [
            "id",
            "external_key",
            "external_url",
            "title",
            "status_category",
            "provider_name",
            "provider_type",
        ]

    def get_provider_name(self, obj) -> str | None:
        return obj.provider.name if obj.provider_id else None

    def get_provider_type(self, obj) -> str | None:
        return obj.provider.provider_type if obj.provider_id else None


class WorkItemLinkCreateSerializer(serializers.ModelSerializer):

    """Serializer for POST /findings/<id>/work-items/."""

    class Meta:
        model = WorkItemLink
        fields = [
            "provider",
            "external_id",
            "external_key",
            "external_url",
            "title",
        ]

    def validate(self, attrs):
        provider = attrs.get("provider")
        external_key = attrs.get("external_key", "")
        # For manual links (no provider) external_url is mandatory.
        if not provider and not attrs.get("external_url"):
            raise serializers.ValidationError(
                {"external_url": "external_url is required when no provider is set."},
            )
        # Uniqueness for manual links (provider=None) is not enforced by the DB
        # unique_together (NULL != NULL in SQL), so we guard here.
        if not provider and external_key:
            finding = self.context["finding"]
            if WorkItemLink.objects.filter(finding=finding, provider__isnull=True, external_key=external_key).exists():
                raise serializers.ValidationError(
                    {"external_key": "A manual link with this external_key already exists for this finding."},
                )
        return attrs

    def create(self, validated_data):
        validated_data["finding"] = self.context["finding"]
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)


class WorkItemLinkUpdateSerializer(serializers.ModelSerializer):

    """Serializer for PATCH /findings/<id>/work-items/<lid>/."""

    class Meta:
        model = WorkItemLink
        fields = [
            "external_key",
            "external_url",
            "title",
            "raw_status",
            "status_category",
        ]


# ---------------------------------------------------------------------------
# Provider views
# ---------------------------------------------------------------------------


class WorkItemProviderListCreateAPI(AuthorizedQuerySetMixin, APIView):

    """
    GET  /organizations/<org_id>/work-item-providers/   — list providers for an org
    POST /organizations/<org_id>/work-item-providers/   — create a new provider
    """

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_work_item_providers,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        tags=[AISTApiTag.WORK_ITEMS],
        summary="List work-item providers for an organization",
        parameters=[
            OpenApiParameter("org_id", int, OpenApiParameter.PATH),
        ],
        responses={200: WorkItemProviderSerializer(many=True)},
    )
    def get(self, request, org_id: int):
        org = get_object_or_404(
            get_authorized_aist_organizations(Permissions.Product_View, user=request.user),
            pk=org_id,
        )
        qs = org.work_item_providers.all()
        return Response(WorkItemProviderSerializer(qs, many=True).data)

    @extend_schema(
        tags=[AISTApiTag.WORK_ITEMS],
        summary="Create a work-item provider",
        parameters=[
            OpenApiParameter("org_id", int, OpenApiParameter.PATH),
        ],
        request=WorkItemProviderSerializer,
        responses={
            201: WorkItemProviderSerializer,
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
        serializer = WorkItemProviderSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class WorkItemProviderDetailAPI(AuthorizedQuerySetMixin, APIView):

    """
    GET    /work-item-providers/<provider_id>/
    PATCH  /work-item-providers/<provider_id>/
    DELETE /work-item-providers/<provider_id>/
    """

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_work_item_providers,
        permission=Permissions.Product_Type_Manage_Members,
    )

    def _get_provider(self, provider_id: int) -> WorkItemProvider:
        return self.get_authorized_object(pk=provider_id)

    @extend_schema(
        tags=[AISTApiTag.WORK_ITEMS],
        summary="Retrieve a work-item provider",
        responses={200: WorkItemProviderSerializer},
    )
    def get(self, request, provider_id: int):
        provider = self._get_provider(provider_id)
        return Response(WorkItemProviderSerializer(provider).data)

    @extend_schema(
        tags=[AISTApiTag.WORK_ITEMS],
        summary="Update a work-item provider",
        request=WorkItemProviderSerializer,
        responses={200: WorkItemProviderSerializer, 400: OpenApiResponse(description="Validation error")},
    )
    def patch(self, request, provider_id: int):
        provider = self._get_provider(provider_id)
        serializer = WorkItemProviderSerializer(provider, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @extend_schema(
        tags=[AISTApiTag.WORK_ITEMS],
        summary="Delete a work-item provider",
        responses={204: None},
    )
    def delete(self, request, provider_id: int):
        self._get_provider(provider_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def _validate_work_item_provider(provider: WorkItemProvider) -> tuple[bool, str]:
    """
    Run credential validation for a WorkItemProvider, routing through VPN when configured.

    Called by the validate_work_item_provider Celery task (runs in the worker
    process which has Docker socket access for vpn_sidecar_context).
    Returns (valid, detail) — never raises.
    """
    execution_id = f"wip-validate-{provider.pk}-{uuid.uuid4().hex[:8]}"
    try:
        backend = get_backend(provider)
        with backend.scoped_context(execution_id=execution_id) as b:
            valid = b.validate_credentials()
            detail = "" if valid else "Credentials are invalid or connectivity check failed."
            return valid, detail
    except NotImplementedError:
        return False, "This provider type does not support credential validation."
    except Exception as exc:
        logger.exception("WorkItemProvider[%s] validation error", provider.pk)
        return False, f"Validation failed ({type(exc).__name__}) — see server logs."


class WorkItemProviderValidateAPI(AuthorizedQuerySetMixin, APIView):

    """
    POST /work-item-providers/<provider_id>/validate/

    Dispatches credential validation to a Celery worker (which has Docker socket
    access for VPN-routed validation).  Returns 202 with a task_id to poll.
    """

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_work_item_providers,
        permission=Permissions.Product_Type_Manage_Members,
    )

    @extend_schema(
        tags=[AISTApiTag.WORK_ITEMS],
        summary="Validate provider credentials (async)",
        request=None,
        responses={
            202: {"type": "object", "properties": {"task_id": {"type": "string"}}},
        },
    )
    def post(self, request, provider_id: int):
        provider = self.get_authorized_object(pk=provider_id)
        from aist.tasks.validate import validate_work_item_provider  # noqa: PLC0415

        result = validate_work_item_provider.delay(provider.pk)
        return Response({"task_id": result.id}, status=status.HTTP_202_ACCEPTED)


class WorkItemProviderValidateStatusAPI(AuthorizedQuerySetMixin, APIView):

    """
    GET /work-item-providers/<provider_id>/validate/<task_id>/

    Polls the result of a validation task dispatched by WorkItemProviderValidateAPI.
    Embeds provider_id in the task result so cross-task result disclosure is prevented:
    a task_id that belongs to a different provider returns PENDING.
    """

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_work_item_providers,
        permission=Permissions.Product_Type_Manage_Members,
    )

    @extend_schema(
        tags=[AISTApiTag.WORK_ITEMS],
        summary="Poll validation task status",
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
    def get(self, request, provider_id: int, task_id: str):
        self.get_authorized_object(pk=provider_id)
        ar = AsyncResult(task_id)
        if ar.state == "SUCCESS":
            result = ar.result or {}
            if result.get("_provider_id") != provider_id:
                return Response({"state": "PENDING", "valid": None, "detail": ""})
            return Response({"state": "SUCCESS", "valid": result["valid"], "detail": result["detail"]})
        if ar.state == "FAILURE":
            meta = ar.result if isinstance(ar.result, dict) else {}
            if meta.get("_provider_id") != provider_id:
                return Response({"state": "PENDING", "valid": None, "detail": ""})
            return Response({"state": "FAILURE", "valid": False, "detail": "Validation failed — see server logs."})
        return Response({"state": ar.state, "valid": None, "detail": ""})


class WorkItemProviderSyncAPI(AuthorizedQuerySetMixin, APIView):

    """
    POST /work-item-providers/<provider_id>/sync/

    Manually triggers a status-sync task for the provider.
    The task runs asynchronously; returns 202 immediately.
    """

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_work_item_providers,
        permission=Permissions.Product_Type_Manage_Members,
    )

    @extend_schema(
        tags=[AISTApiTag.WORK_ITEMS],
        summary="Trigger a manual status sync for a provider",
        request=None,
        responses={
            202: {"type": "object", "properties": {"queued": {"type": "boolean"}}},
        },
    )
    def post(self, request, provider_id: int):
        provider = self.get_authorized_object(pk=provider_id)
        sync_work_item_provider.delay(provider.pk)
        return Response({"queued": True}, status=status.HTTP_202_ACCEPTED)


# ---------------------------------------------------------------------------
# Link views
# ---------------------------------------------------------------------------


class FindingWorkItemListCreateAPI(AuthorizedQuerySetMixin, APIView):

    """
    GET  /findings/<finding_id>/work-items/   — list all links for a finding
    POST /findings/<finding_id>/work-items/   — create a new link
    """

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_findings,
        permission=Permissions.Product_View,
    )

    def _get_finding(self, finding_id: int):
        return self.get_authorized_object(pk=finding_id)

    @extend_schema(
        tags=[AISTApiTag.WORK_ITEMS],
        summary="List work-item links for a finding",
        parameters=[OpenApiParameter("finding_id", int, OpenApiParameter.PATH)],
        responses={200: WorkItemLinkSerializer(many=True)},
    )
    def get(self, request, finding_id: int):
        finding = self._get_finding(finding_id)
        links = WorkItemLink.objects.filter(finding=finding).select_related("provider")
        return Response(WorkItemLinkSerializer(links, many=True).data)

    @extend_schema(
        tags=[AISTApiTag.WORK_ITEMS],
        summary="Create a work-item link for a finding",
        parameters=[OpenApiParameter("finding_id", int, OpenApiParameter.PATH)],
        request=WorkItemLinkCreateSerializer,
        responses={
            201: WorkItemLinkSerializer,
            400: OpenApiResponse(description="Validation error"),
            403: OpenApiResponse(description="Forbidden"),
        },
    )
    def post(self, request, finding_id: int):
        finding = self._get_finding(finding_id)

        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)

        # If provider is given, verify user can access it
        provider_id = data.get("provider")
        if provider_id:
            get_object_or_404(
                get_authorized_work_item_providers(Permissions.Product_Type_Manage_Members, user=request.user),
                pk=provider_id,
            )
        elif data.get("external_url"):
            # Auto-detect provider from URL hostname matching base_url
            try:
                url_host = urlparse(data["external_url"]).netloc.lower()
            except Exception:
                url_host = ""
            if url_host:
                providers_qs = get_authorized_work_item_providers(
                    Permissions.Product_Type_Manage_Members,
                    user=request.user,
                )
                for provider in providers_qs.exclude(base_url=""):
                    try:
                        provider_host = urlparse(provider.base_url).netloc.lower()
                    except Exception:  # noqa: S112
                        continue
                    if provider_host and url_host == provider_host:
                        data["provider"] = provider.pk
                        break

        serializer = WorkItemLinkCreateSerializer(
            data=data,
            context={"finding": finding, "request": request},
        )
        serializer.is_valid(raise_exception=True)
        link = serializer.save()
        if link.provider_id:
            sync_work_item_link.delay(link.pk)
        return Response(
            WorkItemLinkSerializer(link).data,
            status=status.HTTP_201_CREATED,
        )


class FindingWorkItemDetailAPI(AuthorizedQuerySetMixin, APIView):

    """
    PATCH  /findings/<finding_id>/work-items/<link_id>/
    DELETE /findings/<finding_id>/work-items/<link_id>/
    """

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_findings,
        permission=Permissions.Product_View,
    )

    def _get_link(self, finding_id: int, link_id: int) -> WorkItemLink:
        finding = self.get_authorized_object(pk=finding_id)
        return get_object_or_404(WorkItemLink, pk=link_id, finding=finding)

    @extend_schema(
        tags=[AISTApiTag.WORK_ITEMS],
        summary="Update a work-item link",
        request=WorkItemLinkUpdateSerializer,
        responses={200: WorkItemLinkSerializer, 400: OpenApiResponse(description="Validation error")},
    )
    def patch(self, request, finding_id: int, link_id: int):
        link = self._get_link(finding_id, link_id)
        serializer = WorkItemLinkUpdateSerializer(link, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(WorkItemLinkSerializer(link).data)

    @extend_schema(
        tags=[AISTApiTag.WORK_ITEMS],
        summary="Delete a work-item link",
        responses={204: None},
    )
    def delete(self, request, finding_id: int, link_id: int):
        link = self._get_link(finding_id, link_id)
        link.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# Public API choices for use in schema/docs
WORK_ITEM_PROVIDER_TYPES = [t.value for t in WorkItemProviderType]
WORK_ITEM_STATUS_CATEGORIES = [c.value for c in WorkItemStatusCategory]
