from __future__ import annotations

import json

from django.db import transaction
from django.http import HttpRequest, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404
from dojo.authorization.authorization import user_has_permission_or_403
from dojo.authorization.roles_permissions import Permissions
from dojo.models import Finding
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.logging_transport import install_pipeline_logging
from aist.models import AISTPipeline, AISTStatus
from aist.queries import get_authorized_aist_pipelines
from aist.tasks import push_request_to_ai
from aist.utils.pipeline import set_pipeline_status


def send_request_to_ai_for_pipeline(request: HttpRequest, pipeline: AISTPipeline) -> JsonResponse:
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        data = {}

    ids = data.get("finding_ids") or []
    if not isinstance(ids, list) or not all(str(x).isdigit() for x in ids):
        return HttpResponseBadRequest("finding_ids must be a list of integers")

    ids_int = [int(x) for x in ids]
    product = pipeline.project.product

    allowed_qs = Finding.objects.filter(
        id__in=ids_int,
        test__engagement__product=product,
    ).select_related("test__test_type")
    found_ids = list(allowed_qs.values_list("id", flat=True))

    filters = data.get("filters") or {}
    if not found_ids:
        return HttpResponseBadRequest("No valid findings for this pipeline/product")

    try:
        logger = install_pipeline_logging(pipeline.id)
        with transaction.atomic():
            locked = (
                AISTPipeline.objects
                .select_for_update()
                .get(id=pipeline.id)
            )
            if locked.status != AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI:
                logger.error("Attempt to push to AI before receiving confirmation")
                return JsonResponse(
                    {"error": "Attempt to push to AI before receiving confirmation"},
                    status=400,
                )
            set_pipeline_status(locked, AISTStatus.PUSH_TO_AI)

        push_request_to_ai.delay(pipeline.id, ids_int, filters)
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)

    return JsonResponse({"ok": True, "count": len(found_ids)})


def delete_ai_response_for_pipeline(pipeline: AISTPipeline, response_id: int) -> None:
    resp = pipeline.ai_responses.get(id=response_id)
    resp.delete()


class AISendRequestSerializer(serializers.Serializer):
    finding_ids = serializers.ListField(child=serializers.IntegerField(), required=True)
    filters = serializers.JSONField(required=False)


class AISendRequestAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=AISendRequestSerializer,
        responses={200: OpenApiResponse(description="AI request queued")},
    )
    def post(self, request, pipeline_id: str):
        pipeline = get_object_or_404(
            get_authorized_aist_pipelines(Permissions.Product_Edit, user=request.user),
            id=pipeline_id,
        )
        user_has_permission_or_403(request.user, pipeline.project.product, Permissions.Product_Edit)
        return send_request_to_ai_for_pipeline(request, pipeline)


class AIDeleteResponseAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={204: OpenApiResponse(description="Deleted")})
    def delete(self, request, pipeline_id: str, response_id: int):
        pipeline = get_object_or_404(
            get_authorized_aist_pipelines(Permissions.Product_Edit, user=request.user),
            id=pipeline_id,
        )
        user_has_permission_or_403(request.user, pipeline.project.product, Permissions.Product_Edit)
        delete_ai_response_for_pipeline(pipeline, response_id)
        return Response(status=204)
