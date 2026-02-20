from __future__ import annotations

from django.db.models import Q
from django.utils import timezone
from dojo.authorization.authorization import user_has_permission_or_403
from dojo.authorization.roles_permissions import Permissions
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.query import AuthorizedQuerySetMixin, AuthorizedQuerysetSpec
from aist.queries import get_authorized_aist_queue_items


class PipelineLaunchQueueListAPI(AuthorizedQuerySetMixin, APIView):

    """Backend list for UI Queue tab. Supports only_pending and limit."""

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_queue_items,
        permission=Permissions.Product_View,
    )

    class QuerySerializer(serializers.Serializer):
        only_pending = serializers.BooleanField(required=False, default=False)
        limit = serializers.IntegerField(required=False, min_value=1, max_value=2000, default=200)

    @extend_schema(
        tags=["aist"],
        summary="List pipeline launch queue items",
        parameters=[
            OpenApiParameter(name="only_pending", required=False, type=bool),
            OpenApiParameter(name="limit", required=False, type=int),
        ],
        responses={200: OpenApiResponse(description="List")},
    )
    def get(self, request, *args, **kwargs):
        query_serializer = self.QuerySerializer(data=request.query_params)
        query_serializer.is_valid(raise_exception=True)
        only_pending = query_serializer.validated_data["only_pending"]
        limit = query_serializer.validated_data["limit"]

        qs = (
            self.get_authorized_queryset()
            .select_related("project__product", "schedule", "launch_config", "pipeline")
            .order_by("-created")
        )
        if only_pending:
            qs = qs.filter(dispatched=False)

        results = []
        now = timezone.now()
        for q in qs[:limit]:
            project_name = getattr(getattr(q.project, "product", None), "name", str(q.project_id))
            age_seconds = max(0, int((now - q.created).total_seconds()))
            results.append(
                {
                    "id": q.id,
                    "created": q.created,
                    "project_id": q.project_id,
                    "project_name": project_name,
                    "schedule_id": q.schedule_id,
                    "launch_config_id": q.launch_config_id,
                    "dispatched": q.dispatched,
                    "dispatched_at": q.dispatched_at,
                    "pipeline_id": getattr(q.pipeline, "id", None),
                    "age_seconds": age_seconds,
                },
            )

        return Response({"results": results}, status=status.HTTP_200_OK)


class PipelineLaunchQueueClearSerializer(serializers.Serializer):
    days = serializers.IntegerField(min_value=1, max_value=365)


class PipelineLaunchQueueClearDispatchedAPI(AuthorizedQuerySetMixin, APIView):

    """Safe maintenance endpoint: delete dispatched queue items older than X days."""

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_queue_items,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        tags=["aist"],
        summary="Delete dispatched queue items older than X days",
        request=PipelineLaunchQueueClearSerializer,
        responses={200: OpenApiResponse(description="Deleted count")},
    )
    def post(self, request, *args, **kwargs):
        s = PipelineLaunchQueueClearSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        days = s.validated_data["days"]

        cutoff = timezone.now() - timezone.timedelta(days=days)

        deleted, _ = (
            self.get_authorized_queryset(permission=Permissions.Product_Edit)
            .filter(dispatched=True)
            .filter(
                Q(dispatched_at__lt=cutoff)
                | Q(dispatched_at__isnull=True, created__lt=cutoff),
            )
            .delete()
        )
        return Response({"deleted": deleted, "days": days}, status=status.HTTP_200_OK)


class PipelineLaunchQueueDetailAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_queue_items,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        tags=["aist"],
        summary="Delete pipeline launch queue item by id",
        responses={204: OpenApiResponse(description="Deleted"), 404: OpenApiResponse(description="Not found")},
    )
    def delete(self, request, queue_id: int, *args, **kwargs):
        obj = self.get_authorized_object(permission=Permissions.Product_Edit, id=queue_id)
        user_has_permission_or_403(request.user, obj.project.product, Permissions.Product_Edit)
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
