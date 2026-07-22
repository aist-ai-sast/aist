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
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.query import AuthorizedQuerySetMixin, AuthorizedQuerysetSpec
from aist.api.schema import AISTApiTag
from aist.logging_transport import install_pipeline_logging
from aist.models import AISTAIFindingResponse, AISTAIResponse, AISTPipeline, AISTStatus
from aist.queries import get_authorized_aist_pipelines, get_authorized_findings
from aist.tasks import push_request_to_ai, push_request_to_local_triage
from aist.tasks.ai import _resolve_triage_type
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
        early_response = _push_pipeline_to_ai(pipeline, ids_int, filters)
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)
    if early_response is not None:
        return early_response

    return JsonResponse({"ok": True, "count": len(found_ids)})


def _push_pipeline_to_ai(pipeline: AISTPipeline, ids_int: list[int], filters: dict) -> JsonResponse | None:
    """Push confirmed findings to AI triage; returns an early-exit response, or None to continue."""
    logger = install_pipeline_logging(pipeline.id)
    status_ok = True
    with transaction.atomic():
        locked = (
            AISTPipeline.objects
            .select_for_update()
            .get(id=pipeline.id)
        )
        if locked.status != AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI:
            logger.error("Attempt to push to AI before receiving confirmation")
            status_ok = False
        else:
            set_pipeline_status(locked, AISTStatus.PUSH_TO_AI)

    if not status_ok:
        finish_pipeline(pipeline.id, degraded=True)
        return JsonResponse(
            {"error": "Attempt to push to AI before receiving confirmation"},
            status=400,
        )
    triage_type = _resolve_triage_type(locked)
    if triage_type == "local":
        push_request_to_local_triage.delay(pipeline.id, ids_int)
    else:
        push_request_to_ai.delay(pipeline.id, ids_int, filters)
    return None


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
        tags=[AISTApiTag.AI.value],
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

    @extend_schema(
        responses={204: OpenApiResponse(description="Deleted")},
        tags=[AISTApiTag.AI.value],
    )
    def delete(self, request, pipeline_id: str, response_id: int):
        pipeline = self.get_authorized_object(permission=Permissions.Product_Edit, id=pipeline_id)
        user_has_permission_or_403(request.user, pipeline.project.product, Permissions.Product_Edit)
        delete_ai_response_for_pipeline(pipeline, response_id)
        return Response(status=204)


class AIPipelineCallbackAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=AIPipelineCallbackSerializer,
        responses={200: OpenApiResponse(description="Callback accepted")},
        tags=[AISTApiTag.AI.value],
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

        finish_pipeline(pipeline_id, degraded=has_errors)
        return Response({"ok": True})


class LocalTriageCompleteSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["success", "error"], required=True)
    detail = serializers.CharField(required=False, allow_blank=True, default="")


class LocalTriageCompleteAPI(AuthorizedQuerySetMixin, APIView):

    """
    Callback endpoint for the local triage bridge.

    Unlike ``AIPipelineCallbackAPI``, this does NOT run ``sync_ai_finding_responses``
    because the Claude skill writes ``AISTAIFindingResponse`` records directly to the DB.
    This endpoint only calls ``finish_pipeline()``.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=LocalTriageCompleteSerializer,
        responses={200: OpenApiResponse(description="Pipeline finished")},
        tags=[AISTApiTag.AI.value],
    )
    def post(self, request, pipeline_id: str):
        get_object_or_404(AISTPipeline, id=pipeline_id)
        serializer = LocalTriageCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        logger = install_pipeline_logging(pipeline_id)
        status = serializer.validated_data["status"]
        detail = serializer.validated_data.get("detail", "")
        bridge_failed = status == "error"

        degraded = bridge_failed
        if bridge_failed:
            has_responses = AISTAIFindingResponse.objects.filter(pipeline_id=pipeline_id).exists()
            if has_responses:
                # Triage facts are already persisted — the bridge just failed
                # to observe completion (e.g. claude -p lingered past its
                # result event and timed out). Don't mask successful work as
                # degraded; log the bridge error separately for diagnostics.
                logger.error(
                    "Local triage bridge reported error but AI responses exist for pipeline %s; "
                    "finishing as non-degraded. Bridge detail: %s",
                    pipeline_id, detail or "<empty>",
                )
                degraded = False
            elif detail:
                logger.error("Local triage bridge reported error: %s", detail)
            else:
                logger.error("Local triage bridge reported error with no detail")

        finish_pipeline(pipeline_id, degraded=degraded)
        return Response({"ok": True})


class AiFixSerializer(serializers.Serializer):
    fixSummary = serializers.CharField()
    fixType = serializers.CharField()
    diff = serializers.CharField(allow_null=True, default=None)
    diffAvailable = serializers.BooleanField()
    codeAfter = serializers.CharField(allow_null=True, default=None)
    stepByStep = serializers.ListField(child=serializers.CharField())
    testingHint = serializers.CharField(allow_null=True, default=None)
    secretsManagement = serializers.CharField(allow_null=True, default=None)
    suppressionAnnotation = serializers.CharField(allow_null=True, default=None)


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
    fix = AiFixSerializer(allow_null=True)
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
        tags=[AISTApiTag.AI.value],
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
            "fix": row.fix,
            "created": row.created,
        } for row in rows]
        return Response(payload)
