from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db.models import Count, Q
from django_filters import rest_framework as django_filters
from dojo.authorization.roles_permissions import Permissions
from dojo.models import Finding
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.query import AuthorizedQuerySetMixin, AuthorizedQuerysetSpec
from aist.api.schema import AISTApiTag
from aist.models import AISTPipeline, AISTStatus
from aist.queries import get_authorized_aist_pipelines
from aist.utils.project_version_refs import resolve_project_version_git_refs


@dataclass(frozen=True, slots=True)
class PipelineSummaryApiChoices:
    status: list[str]
    ordering: list[str]


PIPELINE_SUMMARY_API_CHOICES = PipelineSummaryApiChoices(
    status=[status for status, _label in AISTStatus.choices],
    ordering=["created", "-created", "updated", "-updated"],
)


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


class AISTPipelineSummaryAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_pipelines,
        permission=Permissions.Product_View,
    )

    class FilterSet(django_filters.FilterSet):
        project_id = django_filters.NumberFilter(field_name="project_id")
        status = django_filters.ChoiceFilter(field_name="status", choices=AISTStatus.choices)
        created_gte = django_filters.IsoDateTimeFilter(field_name="created", lookup_expr="gte")
        created_lte = django_filters.IsoDateTimeFilter(field_name="created", lookup_expr="lte")
        search = django_filters.CharFilter(method="filter_search")
        ordering = django_filters.OrderingFilter(
            fields=(
                ("created", "created"),
                ("updated", "updated"),
            ),
        )

        class Meta:
            model = AISTPipeline
            fields = ("project_id", "status", "created_gte", "created_lte", "search", "ordering")

        def filter_search(self, queryset, _name, value):
            search = (value or "").strip()
            if not search:
                return queryset
            return queryset.filter(
                Q(project_version__version__icontains=search)
                | Q(project_version__resolved_from_branch__version__icontains=search),
            )

    @extend_schema(
        tags=[AISTApiTag.PIPELINES.value],
        summary="List pipeline summaries",
        parameters=[
            OpenApiParameter(name="project_id", required=False, type=int),
            OpenApiParameter(name="status", required=False, type=str, enum=PIPELINE_SUMMARY_API_CHOICES.status),
            OpenApiParameter(name="created_gte", required=False, type=str),
            OpenApiParameter(name="created_lte", required=False, type=str),
            OpenApiParameter(name="search", required=False, type=str),
            OpenApiParameter(name="ordering", required=False, type=str, enum=PIPELINE_SUMMARY_API_CHOICES.ordering),
            OpenApiParameter(name="limit", required=False, type=int),
            OpenApiParameter(name="offset", required=False, type=int),
        ],
        responses={200: AISTPipelineSummaryRowSerializer(many=True)},
    )
    def get(self, request, *args, **kwargs) -> Response:
        filterset = self.FilterSet(
            data=request.query_params,
            queryset=(
                self.get_authorized_queryset()
                .select_related("project", "project__product", "project_version", "project_version__resolved_from_branch")
                .order_by("-created")
            ),
            request=request,
        )
        if not filterset.is_valid():
            return Response(filterset.errors, status=status.HTTP_400_BAD_REQUEST)
        qs = filterset.qs.distinct()

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
