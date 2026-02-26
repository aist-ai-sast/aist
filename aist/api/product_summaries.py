from __future__ import annotations

from typing import Any

from django.db.models import Count, DateTimeField, OuterRef, Q, Subquery
from dojo.authorization.roles_permissions import Permissions
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.common import API_SEVERITY_VALUES, empty_severity_counts
from aist.api.query import AuthorizedQuerySetMixin, AuthorizedQuerysetSpec
from aist.models import AISTPipeline
from aist.queries import get_authorized_aist_projects, get_authorized_findings


class AISTProductSummaryRowSerializer(serializers.Serializer):
    project_id = serializers.IntegerField()
    product_id = serializers.IntegerField()
    product_name = serializers.CharField()
    tags = serializers.ListField(child=serializers.CharField())
    status = serializers.CharField()
    findings_total = serializers.IntegerField()
    findings_active = serializers.IntegerField()
    severity = serializers.JSONField()
    risk = serializers.JSONField()
    last_pipeline = serializers.JSONField()
    last_sync = serializers.DateTimeField(allow_null=True)


class AISTProductSummaryAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_projects,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        tags=["aist"],
        summary="List product summaries",
        responses={200: AISTProductSummaryRowSerializer(many=True)},
    )
    def get(self, request, *args, **kwargs) -> Response:
        projects = (
            self.get_authorized_queryset()
            .select_related("product")
            .prefetch_related("product__tags")
            .order_by("product__name")
        )

        product_ids = [project.product_id for project in projects]

        findings = self.get_authorized_queryset(
            getter=get_authorized_findings,
            permission=Permissions.Finding_View,
        ).filter(
            test__engagement__product_id__in=product_ids,
        )
        findings = findings.order_by()

        severity_annotations = {
            f"severity_{severity.lower()}": Count("id", filter=Q(severity=severity))
            for severity in API_SEVERITY_VALUES
        }
        counts = findings.values("test__engagement__product_id").annotate(
            total=Count("id"),
            active=Count("id", filter=Q(active=True)),
            risk_accepted=Count("id", filter=Q(risk_accepted=True)),
            under_review=Count("id", filter=Q(under_review=True)),
            mitigated=Count("id", filter=Q(is_mitigated=True)),
            **severity_annotations,
        )

        counts_by_product = {row["test__engagement__product_id"]: row for row in counts}

        latest_pipeline = AISTPipeline.objects.filter(project_id=OuterRef("id")).order_by("-updated", "-created")
        projects = projects.annotate(
            last_pipeline_id=Subquery(latest_pipeline.values("id")[:1]),
            last_pipeline_status=Subquery(latest_pipeline.values("status")[:1]),
            last_pipeline_updated=Subquery(
                latest_pipeline.values("updated")[:1],
                output_field=DateTimeField(),
            ),
        )

        results: list[dict[str, Any]] = []
        for project in projects:
            row = counts_by_product.get(project.product_id, {})
            severity = empty_severity_counts()
            for level in API_SEVERITY_VALUES:
                severity[level] = row.get(f"severity_{level.lower()}", 0)
            active_count = row.get("active", 0)
            last_pipeline_at = project.last_pipeline_updated or project.updated
            results.append(
                {
                    "project_id": project.id,
                    "product_id": project.product_id,
                    "product_name": project.product.name,
                    "tags": list(project.product.tags.all().values_list("name", flat=True)),
                    "status": "active" if active_count else "inactive",
                    "findings_total": row.get("total", 0),
                    "findings_active": active_count,
                    "severity": severity,
                    "risk": {
                        "risk_accepted": row.get("risk_accepted", 0),
                        "under_review": row.get("under_review", 0),
                        "mitigated": row.get("mitigated", 0),
                    },
                    "last_pipeline": {
                        "id": project.last_pipeline_id,
                        "status": project.last_pipeline_status,
                        "updated": project.last_pipeline_updated,
                    },
                    "last_sync": last_pipeline_at,
                },
            )

        return Response({"results": results})
