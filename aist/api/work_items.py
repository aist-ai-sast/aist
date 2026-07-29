from __future__ import annotations

import logging
import uuid
from urllib.parse import urlparse

from celery.result import AsyncResult
from django.shortcuts import get_object_or_404
from dojo.models import Finding
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response

from aist.api.schema import AISTApiTag
from aist.authz import Action, AISTAPIView, ResourcePolicy, queryset_for_action
from aist.models import (
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    WorkItemLink,
    WorkItemProvider,
    WorkItemProviderType,
    WorkItemStatusCategory,
)
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
        read_only_fields = ["id", "organization", "created", "updated"]

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
        organization = self.context.get("organization")
        if organization is None and self.instance is not None:
            organization = self.instance.organization
        if organization is not None and value.organization_id != organization.id:
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

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request is not None and request.user.is_authenticated:
            fields["provider"].queryset = queryset_for_action(
                resource=WorkItemProvider,
                action=Action.ORG_READ,
                user=request.user,
            )
        else:
            fields["provider"].queryset = WorkItemProvider.objects.none()
        return fields

    def validate(self, attrs):
        provider = attrs.get("provider")
        request = self.context["request"]
        if provider is not None:
            finding = self.context["finding"]
            finding_organization_id = (
                Organization.objects
                .filter(product_type_id=finding.test.engagement.product.prod_type_id)
                .values_list("id", flat=True)
                .first()
            )
            if provider.organization_id != finding_organization_id:
                raise serializers.ValidationError({
                    "provider": "Provider must belong to the finding's organization.",
                })
        elif attrs.get("external_url"):
            try:
                url_host = urlparse(attrs["external_url"]).netloc.lower()
            except Exception:
                url_host = ""
            if url_host:
                providers = queryset_for_action(
                    resource=WorkItemProvider,
                    action=Action.ORG_READ,
                    user=request.user,
                ).exclude(base_url="")
                for candidate in providers:
                    try:
                        provider_host = urlparse(candidate.base_url).netloc.lower()
                    except Exception:  # noqa: S112
                        continue
                    if provider_host and url_host == provider_host:
                        attrs["provider"] = candidate
                        provider = candidate
                        break
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


class WorkItemProviderListCreateAPI(AISTAPIView):

    """
    GET  /organizations/<org_id>/work-item-providers/   — list providers for an org
    POST /organizations/<org_id>/work-item-providers/   — create a new provider
    """

    # Scoped by organization: GET needs Product_View, POST (create) needs Manage_Members.
    authz = ResourcePolicy(resource=Organization, read=Action.PRODUCT_READ, write=Action.ORG_MANAGE)

    @extend_schema(
        tags=[AISTApiTag.WORK_ITEMS],
        summary="List work-item providers for an organization",
        parameters=[
            OpenApiParameter("org_id", int, OpenApiParameter.PATH),
        ],
        responses={200: WorkItemProviderSerializer(many=True)},
    )
    def get(self, request, org_id: int):
        org = self.resolve(pk=org_id)
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
        org = self.resolve(pk=org_id)
        serializer = WorkItemProviderSerializer(data=request.data, context={"organization": org})
        serializer.is_valid(raise_exception=True)
        serializer.save(organization=org)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class WorkItemProviderDetailAPI(AISTAPIView):

    """
    GET    /work-item-providers/<provider_id>/
    PATCH  /work-item-providers/<provider_id>/
    DELETE /work-item-providers/<provider_id>/
    """

    authz = ResourcePolicy(resource=WorkItemProvider, read=Action.ORG_MANAGE_READ, write=Action.ORG_MANAGE)

    def _get_provider(self, provider_id: int) -> WorkItemProvider:
        return self.resolve(pk=provider_id)

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
        serializer = WorkItemProviderSerializer(
            provider,
            data=request.data,
            partial=True,
            context={"organization": provider.organization},
        )
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


class WorkItemProviderValidateAPI(AISTAPIView):

    """
    POST /work-item-providers/<provider_id>/validate/

    Dispatches credential validation to a Celery worker (which has Docker socket
    access for VPN-routed validation).  Returns 202 with a task_id to poll.
    """

    authz = ResourcePolicy(resource=WorkItemProvider, read=Action.ORG_MANAGE_READ, write=Action.ORG_MANAGE)

    @extend_schema(
        tags=[AISTApiTag.WORK_ITEMS],
        summary="Validate provider credentials (async)",
        request=None,
        responses={
            202: {"type": "object", "properties": {"task_id": {"type": "string"}}},
        },
    )
    def post(self, request, provider_id: int):
        provider = self.resolve(pk=provider_id)
        from aist.tasks.validate import validate_work_item_provider  # noqa: PLC0415

        result = validate_work_item_provider.delay(provider.pk)
        return Response({"task_id": result.id}, status=status.HTTP_202_ACCEPTED)


class WorkItemProviderValidateStatusAPI(AISTAPIView):

    """
    GET /work-item-providers/<provider_id>/validate/<task_id>/

    Polls the result of a validation task dispatched by WorkItemProviderValidateAPI.
    Embeds provider_id in the task result so cross-task result disclosure is prevented:
    a task_id that belongs to a different provider returns PENDING.
    """

    authz = ResourcePolicy(resource=WorkItemProvider, read=Action.ORG_MANAGE_READ, write=Action.ORG_MANAGE)

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
        self.resolve(pk=provider_id)
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


class WorkItemProviderSyncAPI(AISTAPIView):

    """
    POST /work-item-providers/<provider_id>/sync/

    Manually triggers a status-sync task for the provider.
    The task runs asynchronously; returns 202 immediately.
    """

    authz = ResourcePolicy(resource=WorkItemProvider, read=Action.ORG_MANAGE_READ, write=Action.ORG_MANAGE)

    @extend_schema(
        tags=[AISTApiTag.WORK_ITEMS],
        summary="Trigger a manual status sync for a provider",
        request=None,
        responses={
            202: {"type": "object", "properties": {"queued": {"type": "boolean"}}},
        },
    )
    def post(self, request, provider_id: int):
        provider = self.resolve(pk=provider_id)
        sync_work_item_provider.delay(provider.pk)
        return Response({"queued": True}, status=status.HTTP_202_ACCEPTED)


# ---------------------------------------------------------------------------
# Link views
# ---------------------------------------------------------------------------


class FindingWorkItemListCreateAPI(AISTAPIView):

    """
    GET  /findings/<finding_id>/work-items/   — list all links for a finding
    POST /findings/<finding_id>/work-items/   — create a new link
    """

    # G-1: attaching/editing work-item links is a finding write (Writer+), not a read.
    authz = ResourcePolicy(resource=Finding, read=Action.FINDING_READ, write=Action.FINDING_EDIT)

    def _get_finding(self, finding_id: int):
        return self.resolve(pk=finding_id)

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

        serializer = WorkItemLinkCreateSerializer(
            data=request.data,
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


class FindingWorkItemDetailAPI(AISTAPIView):

    """
    PATCH  /findings/<finding_id>/work-items/<link_id>/
    DELETE /findings/<finding_id>/work-items/<link_id>/
    """

    # G-1: attaching/editing work-item links is a finding write (Writer+), not a read.
    authz = ResourcePolicy(resource=Finding, read=Action.FINDING_READ, write=Action.FINDING_EDIT)

    def _get_link(self, finding_id: int, link_id: int) -> WorkItemLink:
        finding = self.resolve(pk=finding_id)
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
