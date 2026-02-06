from __future__ import annotations

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from dojo.authorization.authorization import user_has_permission_or_403
from dojo.authorization.roles_permissions import Permissions
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.queries import get_authorized_aist_queue_items


class PipelineLaunchQueueListAPI(APIView):

    """Backend list for UI Queue tab. Supports only_pending and limit."""

    permission_classes = [IsAuthenticated]

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
        only_pending = (request.query_params.get("only_pending") or "").lower() in {"1", "true", "yes"}
        try:
            limit = int(request.query_params.get("limit") or 200)
        except ValueError:
            limit = 200
        limit = max(1, min(limit, 2000))

        qs = (
            get_authorized_aist_queue_items(Permissions.Product_View, user=request.user)
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


class PipelineLaunchQueueClearDispatchedAPI(APIView):

    """Safe maintenance endpoint: delete dispatched queue items older than X days."""

    permission_classes = [IsAuthenticated]

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
            get_authorized_aist_queue_items(Permissions.Product_Edit, user=request.user)
            .filter(dispatched=True)
            .filter(
                Q(dispatched_at__lt=cutoff)
                | Q(dispatched_at__isnull=True, created__lt=cutoff),
            )
            .delete()
        )
        return Response({"deleted": deleted, "days": days}, status=status.HTTP_200_OK)


class PipelineLaunchQueueDetailAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="Delete pipeline launch queue item by id",
        responses={204: OpenApiResponse(description="Deleted"), 404: OpenApiResponse(description="Not found")},
    )
    def delete(self, request, queue_id: int, *args, **kwargs):
        obj = get_object_or_404(
            get_authorized_aist_queue_items(Permissions.Product_Edit, user=request.user),
            id=queue_id,
        )
        user_has_permission_or_403(request.user, obj.project.product, Permissions.Product_Edit)
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
