from __future__ import annotations

import json

from django.db import transaction
from django.http import HttpRequest, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404
from dojo.authorization.authorization import user_has_permission_or_403
from dojo.authorization.roles_permissions import Permissions
from dojo.finding.queries import get_authorized_findings
from dojo.models import Finding
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.logging_transport import install_pipeline_logging
from aist.models import AISTAIFindingResponse, AISTAIResponse, AISTPipeline, AISTStatus
from aist.queries import get_authorized_aist_pipelines
from aist.tasks import push_request_to_ai
from aist.utils.ai_response import sync_ai_finding_responses
from aist.utils.pipeline import finish_pipeline, set_pipeline_status


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
                finish_pipeline(locked, degraded=True)
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


class AIPipelineCallbackSerializer(serializers.Serializer):
    errors = serializers.JSONField(required=False)
    results = serializers.JSONField(required=False)


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


class AIPipelineCallbackAPI(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=AIPipelineCallbackSerializer,
        responses={200: OpenApiResponse(description="Callback accepted")},
    )
    def post(self, request, pipeline_id: str):
        get_object_or_404(AISTPipeline, id=pipeline_id)
        response_from_ai = dict(request.data)

        errors = response_from_ai.pop("errors", None)
        logger = install_pipeline_logging(pipeline_id)
        has_errors = bool(errors)
        if errors:
            logger.error(errors)

        with transaction.atomic():
            locked = (
                AISTPipeline.objects
                .select_for_update()
                .get(id=pipeline_id)
            )
            ai_response = AISTAIResponse.objects.create(pipeline=locked, payload=response_from_ai)
            locked.response_from_ai = response_from_ai
            locked.save(update_fields=["response_from_ai", "updated"])
            sync_stats = sync_ai_finding_responses(
                pipeline=locked,
                ai_response=ai_response,
                user=request.user,
            )
            if sync_stats.dropped > 0:
                logger.warning(
                    "Dropped %s AI findings that could not be matched to existing findings.",
                    sync_stats.dropped,
                )
            finish_pipeline(locked, degraded=has_errors)

        return Response({"ok": True})


class AIFindingResponseItemSerializer(serializers.Serializer):
    pipeline_id = serializers.CharField()
    finding_id = serializers.IntegerField()
    verdict = serializers.CharField()
    title = serializers.CharField(allow_blank=True)
    reasoning = serializers.CharField(allow_blank=True)
    epssScore = serializers.FloatField(allow_null=True)
    impactScore = serializers.FloatField(allow_null=True)
    exploitabilityScore = serializers.FloatField(allow_null=True)
    uncertaintyLevel = serializers.FloatField(allow_null=True)
    uncertaintySpread = serializers.FloatField(allow_null=True)
    exploitCodeMaturity = serializers.CharField(allow_blank=True)
    references = serializers.ListField(child=serializers.CharField(), allow_empty=True)
    created = serializers.DateTimeField()


class AIFindingResponseListAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={200: AIFindingResponseItemSerializer(many=True)},
    )
    def get(self, request):
        project_id = request.query_params.get("project_id")
        pipeline_qs = get_authorized_aist_pipelines(
            Permissions.Product_View,
            user=request.user,
        )
        if project_id:
            try:
                project_id_int = int(project_id)
            except (TypeError, ValueError):
                return Response({"detail": "project_id must be an integer"}, status=400)
            pipeline_qs = pipeline_qs.filter(project_id=project_id_int)
        pipeline_id = request.query_params.get("pipeline_id")
        if pipeline_id:
            pipeline_qs = pipeline_qs.filter(id=pipeline_id)

        finding_ids_param = (request.query_params.get("finding_ids") or "").strip()
        finding_ids: list[int] = []
        if finding_ids_param:
            for raw_token in finding_ids_param.split(","):
                token_value = raw_token.strip()
                if not token_value:
                    continue
                try:
                    finding_ids.append(int(token_value))
                except ValueError:
                    continue
        finding_ids = list(dict.fromkeys([value for value in finding_ids if value > 0]))

        if finding_ids:
            allowed_finding_ids = set(
                get_authorized_findings(Permissions.Finding_View, user=request.user)
                .filter(id__in=finding_ids)
                .values_list("id", flat=True),
            )
            if not allowed_finding_ids:
                return Response([])
        else:
            allowed_finding_ids = set()

        qs = (
            AISTAIFindingResponse.objects
            .filter(pipeline__in=pipeline_qs)
            .order_by("-pipeline__created", "-updated", "-id")
        )
        if allowed_finding_ids:
            qs = qs.filter(finding_id__in=allowed_finding_ids)

        rows: list[AISTAIFindingResponse] = []
        if pipeline_id:
            rows = list(qs.select_related("pipeline"))
        else:
            seen_finding_ids: set[int] = set()
            for row in qs.select_related("pipeline"):
                if row.finding_id in seen_finding_ids:
                    continue
                seen_finding_ids.add(row.finding_id)
                rows.append(row)

        payload = [{
            "pipeline_id": row.pipeline_id,
            "finding_id": row.finding_id,
            "verdict": row.verdict,
            "title": row.title,
            "reasoning": row.summary,
            "epssScore": row.epss_score,
            "impactScore": row.impact_score,
            "exploitabilityScore": row.exploitability_score,
            "uncertaintyLevel": row.uncertainty_level,
            "uncertaintySpread": row.uncertainty_spread,
            "exploitCodeMaturity": row.exploit_code_maturity,
            "references": row.references or [],
            "created": row.created,
        } for row in rows]
        return Response(payload)
