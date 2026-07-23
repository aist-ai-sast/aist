from __future__ import annotations

from django.db.models import Q
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response

from aist.api.schema import AISTApiTag
from aist.authz import Action, AISTAPIView, ResourcePolicy
from aist.models import PipelineLaunchQueue

# Read = Product_View (Reader+); write (clear/delete) = Product_Edit (Maintainer+).
_QUEUE_POLICY = ResourcePolicy(
    resource=PipelineLaunchQueue,
    read=Action.PRODUCT_READ,
    write=Action.PROJECT_OPERATE,
)


class PipelineLaunchQueueListAPI(AISTAPIView):

    """Backend list for UI Queue tab. Supports only_pending and limit."""

    authz = _QUEUE_POLICY

    class QuerySerializer(serializers.Serializer):
        only_pending = serializers.BooleanField(required=False, default=False)
        limit = serializers.IntegerField(required=False, min_value=1, max_value=2000, default=200)

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_QUEUE.value],
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
            self.authorized_queryset_for_request()
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


class PipelineLaunchQueueClearDispatchedAPI(AISTAPIView):

    """Safe maintenance endpoint: delete dispatched queue items older than X days."""

    authz = _QUEUE_POLICY

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_QUEUE.value],
        summary="Delete dispatched queue items older than X days",
        request=PipelineLaunchQueueClearSerializer,
        responses={200: OpenApiResponse(description="Deleted count")},
    )
    def post(self, request, *args, **kwargs):
        s = PipelineLaunchQueueClearSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        days = s.validated_data["days"]

        cutoff = timezone.now() - timezone.timedelta(days=days)

        # POST → write action → Product_Edit-scoped queryset.
        deleted, _ = (
            self.authorized_queryset_for_request()
            .filter(dispatched=True)
            .filter(
                Q(dispatched_at__lt=cutoff)
                | Q(dispatched_at__isnull=True, created__lt=cutoff),
            )
            .delete()
        )
        return Response({"deleted": deleted, "days": days}, status=status.HTTP_200_OK)


class PipelineLaunchQueueDetailAPI(AISTAPIView):
    authz = _QUEUE_POLICY

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_QUEUE.value],
        summary="Delete pipeline launch queue item by id",
        responses={204: OpenApiResponse(description="Deleted"), 404: OpenApiResponse(description="Not found")},
    )
    def delete(self, request, queue_id: int, *args, **kwargs):
        # DELETE → write action → resolve enforces Product_Edit on the item's product.
        obj = self.resolve(id=queue_id)
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
