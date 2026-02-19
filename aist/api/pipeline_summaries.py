from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db.models import Count, Q
from dojo.authorization.roles_permissions import Permissions
from dojo.models import Finding
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from aist.queries import get_authorized_aist_pipelines
from aist.utils.project_version_refs import resolve_project_version_git_refs

if TYPE_CHECKING:
    from rest_framework.response import Response


class AISTPipelineSummaryRowSerializer(serializers.Serializer):
    id = serializers.CharField()
    status = serializers.CharField()
    project_id = serializers.IntegerField()
    product_id = serializers.IntegerField()
    product_name = serializers.CharField()
    started = serializers.DateTimeField(allow_null=True)
    created = serializers.DateTimeField()
    updated = serializers.DateTimeField()
    branch = serializers.CharField(allow_null=True)
    commit = serializers.CharField(allow_null=True)
    findings = serializers.IntegerField()
    actions = serializers.JSONField()


class AISTPipelineSummaryAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="List pipeline summaries",
        parameters=[
            OpenApiParameter(name="project_id", required=False, type=int),
            OpenApiParameter(name="status", required=False, type=str),
            OpenApiParameter(name="created_gte", required=False, type=str),
            OpenApiParameter(name="created_lte", required=False, type=str),
            OpenApiParameter(name="search", required=False, type=str),
            OpenApiParameter(name="ordering", required=False, type=str),
            OpenApiParameter(name="limit", required=False, type=int),
            OpenApiParameter(name="offset", required=False, type=int),
        ],
        responses={200: AISTPipelineSummaryRowSerializer(many=True)},
    )
    def get(self, request, *args, **kwargs) -> Response:
        qs = (
            get_authorized_aist_pipelines(Permissions.Product_View, user=request.user)
            .select_related("project", "project__product", "project_version", "project_version__resolved_from_branch")
            .order_by("-created")
        )
        qp = request.query_params

        project_id = qp.get("project_id")
        status = qp.get("status")
        created_gte = qp.get("created_gte")
        created_lte = qp.get("created_lte")
        search = (qp.get("search") or "").strip()
        ordering = qp.get("ordering")

        if project_id:
            qs = qs.filter(project_id=project_id)
        if status:
            qs = qs.filter(status=status)
        if created_gte:
            qs = qs.filter(created__gte=created_gte)
        if created_lte:
            qs = qs.filter(created__lte=created_lte)
        if search:
            qs = qs.filter(
                Q(project_version__version__icontains=search)
                | Q(project_version__resolved_from_branch__version__icontains=search),
            )

        qs = qs.distinct()

        if ordering in {"created", "-created", "updated", "-updated"}:
            qs = qs.order_by(ordering)

        paginator = LimitOffsetPagination()
        page = paginator.paginate_queryset(qs, request)
        pipeline_ids = [pipeline.id for pipeline in page]

        counts: dict[str, int] = {}
        if pipeline_ids:
            counts_qs = (
                Finding.objects.filter(test__aist_pipelines__id__in=pipeline_ids)
                .order_by()
                .values("test__aist_pipelines__id")
                .annotate(total=Count("id"))
            )
            counts = {row["test__aist_pipelines__id"]: row["total"] for row in counts_qs}

        results: list[dict[str, Any]] = []
        for pipeline in page:
            refs = resolve_project_version_git_refs(pipeline.project_version)

            action_runs = (pipeline.launch_data or {}).get("action_runs") or []
            actions = [
                {
                    "source": item.get("source"),
                    "type": item.get("action_type"),
                    "status": item.get("status"),
                    "updated": item.get("updated_at"),
                }
                for item in action_runs
            ]
            results.append(
                {
                    "id": pipeline.id,
                    "status": pipeline.status,
                    "project_id": pipeline.project_id,
                    "product_id": pipeline.project.product_id,
                    "product_name": pipeline.project.product.name,
                    "started": pipeline.started,
                    "created": pipeline.created,
                    "updated": pipeline.updated,
                    "branch": refs.branch,
                    "commit": refs.commit,
                    "findings": counts.get(pipeline.id, 0),
                    "actions": actions,
                },
            )

        return paginator.get_paginated_response(results)
