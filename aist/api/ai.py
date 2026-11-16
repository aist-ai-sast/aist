from __future__ import annotations

from django.db import transaction
from django.http import JsonResponse
from django_filters import rest_framework as django_filters
from dojo.models import Finding
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.response import Response

from aist.api.schema import AISTApiTag
from aist.authz import INTERNAL_SERVICE, Action, AISTAPIView, ResourcePolicy, queryset_for_action
from aist.logging_transport import install_pipeline_logging, scoped_pipeline_logging
from aist.models import AISTAIFindingResponse, AISTAIResponse, AISTPipeline, AISTStatus
from aist.services.ai_triage import PushGuard, push_guard
from aist.services.pipeline_lifecycle import lock_existing_pipeline, lock_pipeline
from aist.tasks.ai import confirm_triage_push
from aist.utils.ai_response import sync_ai_finding_responses
from aist.utils.pipeline import finish_pipeline


def send_request_to_ai_for_pipeline(pipeline: AISTPipeline, user, finding_ids: list[int], filters: dict) -> JsonResponse:
    """Push the findings of ``finding_ids`` that ``user`` may edit in the pipeline's product to AI triage."""
    found_ids = list(
        queryset_for_action(resource=Finding, action=Action.FINDING_EDIT, user=user)
        .filter(id__in=finding_ids, test__engagement__product=pipeline.project.product)
        .values_list("id", flat=True),
    )
    if not found_ids:
        return JsonResponse({"detail": "No valid findings for this pipeline/product"}, status=400)
    early_response = _push_pipeline_to_ai(pipeline, found_ids, filters)
    if early_response is not None:
        return early_response
    return JsonResponse({"ok": True, "count": len(found_ids)})


_PUSH_REFUSALS = {
    PushGuard.UNEXPECTED_STATUS: (400, "The pipeline is not waiting for confirmation to push to AI triage"),
    PushGuard.ALREADY_PUSHED: (409, "The findings of this pipeline were already pushed to AI triage"),
    PushGuard.FINISHED: (409, "The pipeline has finished; it takes no AI triage push"),
    PushGuard.NOT_FOUND: (404, "The pipeline does not exist"),
}


def _push_pipeline_to_ai(pipeline: AISTPipeline, finding_ids: list[int], filters: dict) -> JsonResponse | None:
    """Confirm the push of a pipeline waiting for confirmation; a refusal leaves the pipeline as it is."""
    with scoped_pipeline_logging(pipeline.id) as log, transaction.atomic():
        locked = lock_pipeline(pipeline.id)
        guard = push_guard(locked, pipeline.id, AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI, log)
        if guard is PushGuard.READY:
            confirm_triage_push(locked, finding_ids, filters, log)
            return None
    status_code, message = _PUSH_REFUSALS[guard]
    return JsonResponse({"error": message}, status=status_code)


def delete_ai_response_for_pipeline(pipeline: AISTPipeline, response_id: int) -> None:
    resp = pipeline.ai_responses.get(id=response_id)
    resp.delete()


class AISendRequestSerializer(serializers.Serializer):
    finding_ids = serializers.ListField(child=serializers.IntegerField(), required=True)
    filters = serializers.JSONField(required=False, default=dict)


class AIPipelineCallbackSerializer(serializers.Serializer):
    errors = serializers.JSONField(required=False)
    results = serializers.JSONField(required=False)


class AISendRequestAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        request=AISendRequestSerializer,
        responses={200: OpenApiResponse(description="AI request queued")},
        tags=[AISTApiTag.AI.value],
    )
    def post(self, request, pipeline_id: str):
        pipeline = self.resolve(id=pipeline_id)
        serializer = AISendRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return send_request_to_ai_for_pipeline(
            pipeline,
            request.user,
            serializer.validated_data["finding_ids"],
            serializer.validated_data["filters"],
        )


class AIDeleteResponseAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        responses={204: OpenApiResponse(description="Deleted")},
        tags=[AISTApiTag.AI.value],
    )
    def delete(self, request, pipeline_id: str, response_id: int):
        pipeline = self.resolve(id=pipeline_id)
        delete_ai_response_for_pipeline(pipeline, response_id)
        return Response(status=204)


class AIPipelineCallbackAPI(AISTAPIView):
    authz = INTERNAL_SERVICE

    @extend_schema(
        request=AIPipelineCallbackSerializer,
        responses={200: OpenApiResponse(description="Callback accepted")},
        tags=[AISTApiTag.AI.value],
    )
    def post(self, request, pipeline_id: str):
        pipeline = queryset_for_action(
            resource=AISTPipeline,
            action=Action.PRODUCT_READ,
            user=request.user,
        ).filter(id=pipeline_id).first()
        if pipeline is None:
            return Response({"detail": "Pipeline not found"}, status=404)
        serializer = AIPipelineCallbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        response_from_ai = dict(serializer.validated_data)

        errors = response_from_ai.pop("errors", None)
        logger = install_pipeline_logging(pipeline_id)
        has_errors = bool(errors)
        if errors:
            logger.error(errors)

        with transaction.atomic():
            locked = lock_existing_pipeline(pipeline_id)
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


class AIFindingResponseListAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

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
        pipeline_qs = self.authorized_queryset()
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
                self.authorized_queryset(resource=Finding, action=Action.FINDING_READ)
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
