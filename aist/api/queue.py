from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response

from aist.api.schema import AISTApiTag
from aist.authz import Action, AISTAPIView, ResourcePolicy
from aist.execution.cancellation import cancel_launch_request
from aist.models import PipelineLaunchRequest, PipelineLaunchRequestState
from aist.utils.pipeline import stop_pipeline

# Read = Product_View (Reader+); write (clear/delete) = Product_Edit (Maintainer+).
_LAUNCH_REQUEST_POLICY = ResourcePolicy(
    resource=PipelineLaunchRequest,
    read=Action.PRODUCT_READ,
    write=Action.PROJECT_OPERATE,
)

# A request in one of these states is actively being worked by the claim/plan/dispatch
# pipeline right now; deleting it out from under that machinery would either desync the
# worker or (pre-fix) hit the PROTECT constraint on its execution lease. PENDING has
# never been claimed and DISPATCHED has already handed off to the pipeline object, so
# both are safe to remove directly once any lease they hold has been released.
_DELETE_BLOCKED_STATES = frozenset({
    PipelineLaunchRequestState.CLAIMED,
    PipelineLaunchRequestState.PLANNED,
    PipelineLaunchRequestState.PUBLISHED,
})


def _blocked_from_delete(request: PipelineLaunchRequest) -> bool:
    if request.state in _DELETE_BLOCKED_STATES:
        return True
    return request.execution_leases.filter(released_at__isnull=True).exists()


class PipelineLaunchRequestListAPI(AISTAPIView):

    """Backend list for the admin launch-scheduling dashboard. Supports only_pending and limit."""

    authz = _LAUNCH_REQUEST_POLICY

    class QuerySerializer(serializers.Serializer):
        only_pending = serializers.BooleanField(required=False, default=False)
        limit = serializers.IntegerField(required=False, min_value=1, max_value=2000, default=200)

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_REQUESTS.value],
        summary="List pipeline launch requests",
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
            qs = qs.filter(state=PipelineLaunchRequestState.PENDING)

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
                    "origin": q.origin,
                    "execution_type": q.execution_type,
                    "state": q.state,
                    "not_before": q.not_before,
                    "expires_at": q.expires_at,
                    "capacity_retry_count": q.capacity_retry_count,
                    "failure_code": q.failure_code,
                    "dispatched": q.dispatched,
                    "dispatched_at": q.dispatched_at,
                    "pipeline_id": getattr(q.pipeline, "id", None),
                    "age_seconds": age_seconds,
                },
            )

        return Response({"results": results}, status=status.HTTP_200_OK)


class PipelineLaunchRequestClearSerializer(serializers.Serializer):
    days = serializers.IntegerField(min_value=1, max_value=365)


class PipelineLaunchRequestClearDispatchedAPI(AISTAPIView):

    """Safe maintenance endpoint: delete dispatched launch requests older than X days."""

    authz = _LAUNCH_REQUEST_POLICY

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_REQUESTS.value],
        summary="Delete dispatched launch requests older than X days",
        request=PipelineLaunchRequestClearSerializer,
        responses={200: OpenApiResponse(description="Deleted count")},
    )
    def post(self, request, *args, **kwargs):
        s = PipelineLaunchRequestClearSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        days = s.validated_data["days"]

        cutoff = timezone.now() - timezone.timedelta(days=days)

        # POST → write action → Product_Edit-scoped queryset.
        # Excludes rows still holding an open lease: cascading their deletion would
        # silently drop the capacity-accounting record for a slot that may still be in
        # use (e.g. a dispatched run reconciliation hasn't caught up with yet).
        matched = (
            self.authorized_queryset_for_request()
            .filter(state=PipelineLaunchRequestState.DISPATCHED)
            .filter(
                Q(dispatched_at__lt=cutoff)
                | Q(dispatched_at__isnull=True, created__lt=cutoff),
            )
            .exclude(execution_leases__released_at__isnull=True)
        )
        # Report the request count, not the cascade total: deleting a request's own
        # (already-released) lease rows alongside it shouldn't inflate what an admin
        # reads as "N requests cleared".
        deleted = matched.count()
        matched.delete()
        return Response({"deleted": deleted, "days": days}, status=status.HTTP_200_OK)


class PipelineLaunchRequestDetailAPI(AISTAPIView):
    authz = _LAUNCH_REQUEST_POLICY

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_REQUESTS.value],
        summary="Delete a pipeline launch request by id",
        responses={
            204: OpenApiResponse(description="Deleted"),
            404: OpenApiResponse(description="Not found"),
            409: OpenApiResponse(description="Request is mid-flight or still holds an open execution lease"),
        },
    )
    def delete(self, request, request_id: int, *args, **kwargs):
        # DELETE → write action → resolve enforces Product_Edit on the item's product,
        # tenant-scoped and 404-on-miss before we ever take a row lock.
        self.resolve(id=request_id)
        # Re-check under a row lock in the same transaction as the delete: acquiring an
        # execution lease (leases.py) and claiming a request (claiming.py) both take this
        # same select_for_update() on the request row first, so this serializes against
        # them instead of racing a lease into existence between an unlocked check and the
        # delete (which would otherwise cascade-delete it unnoticed).
        with transaction.atomic():
            locked = PipelineLaunchRequest.objects.select_for_update().filter(pk=request_id).first()
            if locked is None:
                return Response(status=status.HTTP_204_NO_CONTENT)
            if _blocked_from_delete(locked):
                return Response(
                    {"detail": "Cancel the request before deleting it while it is in progress."},
                    status=status.HTTP_409_CONFLICT,
                )
            locked.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PipelineLaunchRequestCancelAPI(AISTAPIView):

    """Cancel a not-yet-dispatched request, or stop the pipeline if already dispatched."""

    authz = _LAUNCH_REQUEST_POLICY

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_REQUESTS.value],
        summary="Cancel a pipeline launch request",
        request=None,
        responses={
            200: OpenApiResponse(description="Cancelled or stop requested"),
            404: OpenApiResponse(description="Not found"),
            409: OpenApiResponse(description="Request already reached a terminal state"),
        },
    )
    def post(self, request, request_id: int, *args, **kwargs):
        # POST → write action → resolve enforces Product_Edit on the item's product.
        obj = self.resolve(id=request_id)
        if obj.state == PipelineLaunchRequestState.DISPATCHED:
            if obj.pipeline_id is None:
                return Response({"detail": "Dispatched request has no pipeline to stop."}, status=status.HTTP_409_CONFLICT)
            stop_pipeline(obj.pipeline)
            return Response({"state": obj.state, "stopped_pipeline_id": obj.pipeline_id}, status=status.HTTP_200_OK)

        cancelled = cancel_launch_request(request_id=obj.pk)
        if not cancelled:
            return Response(
                {"detail": "Request already left a cancellable state."},
                status=status.HTTP_409_CONFLICT,
            )
        return Response({"state": PipelineLaunchRequestState.CANCELLED}, status=status.HTTP_200_OK)
