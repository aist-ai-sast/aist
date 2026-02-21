from __future__ import annotations

import json

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django_filters import rest_framework as django_filters
from dojo.authorization.authorization import user_has_permission_or_403
from dojo.authorization.roles_permissions import Permissions
from dojo.models import Finding
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.query import AuthorizedQuerySetMixin, AuthorizedQuerysetSpec
from aist.logging_transport import install_pipeline_logging
from aist.models import AISTAIFindingResponse, AISTAIResponse, AISTPipeline, AISTStatus
from aist.queries import get_authorized_aist_pipelines, get_authorized_findings
from aist.tasks import push_request_to_ai
from aist.utils.ai_response import sync_ai_finding_responses
from aist.utils.pipeline import finish_pipeline, set_pipeline_status


def send_request_to_ai_for_pipeline(
    request=None,
    pipeline: AISTPipeline | None = None,
    *,
    finding_ids: list[int] | None = None,
    filters: dict | None = None,
) -> JsonResponse:
    if finding_ids is None:
        payload = {}
        if request is not None:
            if hasattr(request, "data"):
                payload = request.data
            else:
                try:
                    payload = json.loads(request.body.decode("utf-8") or "{}")
                except Exception:
                    payload = {}
        serializer = AISendRequestSerializer(data=payload)
        if not serializer.is_valid():
            return JsonResponse(serializer.errors, status=400)
        finding_ids = serializer.validated_data["finding_ids"]
        filters = serializer.validated_data.get("filters") or {}

    if pipeline is None:
        return JsonResponse({"detail": "pipeline is required"}, status=400)

    ids_int = [int(value) for value in finding_ids]
    product = pipeline.project.product

    allowed_qs = Finding.objects.filter(
        id__in=ids_int,
        test__engagement__product=product,
    ).select_related("test__test_type")
    found_ids = list(allowed_qs.values_list("id", flat=True))

    if not found_ids:
        return JsonResponse({"detail": "No valid findings for this pipeline/product"}, status=400)

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
    filters = serializers.JSONField(required=False, default=dict)


class AIPipelineCallbackSerializer(serializers.Serializer):
    errors = serializers.JSONField(required=False)
    results = serializers.JSONField(required=False)


class AISendRequestAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_pipelines,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        request=AISendRequestSerializer,
        responses={200: OpenApiResponse(description="AI request queued")},
    )
    def post(self, request, pipeline_id: str):
        pipeline = self.get_authorized_object(permission=Permissions.Product_Edit, id=pipeline_id)
        user_has_permission_or_403(request.user, pipeline.project.product, Permissions.Product_Edit)
        serializer = AISendRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return send_request_to_ai_for_pipeline(
            pipeline=pipeline,
            finding_ids=serializer.validated_data["finding_ids"],
            filters=serializer.validated_data["filters"],
        )


class AIDeleteResponseAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_pipelines,
        permission=Permissions.Product_View,
    )

    @extend_schema(responses={204: OpenApiResponse(description="Deleted")})
    def delete(self, request, pipeline_id: str, response_id: int):
        pipeline = self.get_authorized_object(permission=Permissions.Product_Edit, id=pipeline_id)
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


class NumberInFilter(django_filters.BaseInFilter, django_filters.NumberFilter):
    pass


class AIFindingResponseListAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_pipelines,
        permission=Permissions.Product_View,
    )

    class FilterSet(django_filters.FilterSet):
        project_id = django_filters.NumberFilter(field_name="pipeline__project_id")
        pipeline_id = django_filters.CharFilter(field_name="pipeline_id")
        finding_ids = NumberInFilter(field_name="finding_id", lookup_expr="in")

        class Meta:
            model = AISTAIFindingResponse
            fields = ("project_id", "pipeline_id", "finding_ids")

    @extend_schema(
        responses={200: AIFindingResponseItemSerializer(many=True)},
    )
    def get(self, request):
        pipeline_qs = self.get_authorized_queryset()
        qs = (
            AISTAIFindingResponse.objects
            .filter(pipeline__in=pipeline_qs)
            .order_by("-pipeline__created", "-updated", "-id")
        )
        filterset = self.FilterSet(data=request.query_params, queryset=qs, request=request)
        if not filterset.is_valid():
            return Response(filterset.errors, status=400)
        qs = filterset.qs

        cleaned = filterset.form.cleaned_data
        pipeline_id = cleaned.get("pipeline_id")
        finding_ids = cleaned.get("finding_ids")

        if finding_ids:
            unique_finding_ids = list(dict.fromkeys([value for value in finding_ids if value > 0]))
            allowed_finding_ids = set(
                self.get_authorized_queryset(
                    getter=get_authorized_findings,
                    permission=Permissions.Finding_View,
                )
                .filter(id__in=unique_finding_ids)
                .values_list("id", flat=True),
            )
            if not allowed_finding_ids:
                return Response([])
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
