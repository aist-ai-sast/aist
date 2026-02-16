from __future__ import annotations

from typing import TYPE_CHECKING

from dojo.api_v2 import serializers as dojo_serializers
from dojo.authorization.roles_permissions import Permissions
from dojo.filters import ApiFindingFilter
from dojo.finding.queries import get_authorized_findings
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from aist.models import VersionType
from aist.queries import get_authorized_aist_pipelines

if TYPE_CHECKING:
    from rest_framework.response import Response


def _parse_tags(request) -> list[str]:
    raw_values = request.query_params.getlist("tags")
    tags: list[str] = []
    for raw in raw_values:
        if not raw:
            continue
        tags.extend([item.strip() for item in raw.split(",") if item.strip()])
    return tags


def _parse_csv_values(request, param_name: str) -> list[str]:
    raw_values = request.query_params.getlist(param_name)
    values: list[str] = []
    for raw in raw_values:
        if not raw:
            continue
        values.extend([item.strip() for item in raw.split(",") if item.strip()])
    return values


def _pick_project_version_label(finding) -> str | None:
    versions_rel = getattr(finding, "aist_project_versions", None)
    if versions_rel is None:
        return None
    versions = list(versions_rel.all())
    if not versions:
        return None
    hash_version = next((v for v in versions if v.version_type == VersionType.GIT_HASH), None)
    if hash_version:
        return hash_version.version
    branch_version = next((v for v in versions if v.version_type == VersionType.GIT_BRANCH), None)
    if branch_version:
        return branch_version.version
    return versions[0].version


class AISTFindingListAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="List AIST findings",
        parameters=[
            OpenApiParameter(name="pipeline_id", required=False, type=str),
            OpenApiParameter(name="tags", required=False, type=str, many=True),
            OpenApiParameter(name="severity", required=False, type=str, many=True),
            OpenApiParameter(name="project_version", required=False, type=str),
            OpenApiParameter(name="file", required=False, type=str),
            OpenApiParameter(name="ordering", required=False, type=str),
            OpenApiParameter(name="limit", required=False, type=int),
            OpenApiParameter(name="offset", required=False, type=int),
        ],
        responses={200: dojo_serializers.FindingSerializer(many=True)},
    )
    def get(self, request, *args, **kwargs) -> Response:
        queryset = get_authorized_findings(Permissions.Finding_View, user=request.user).prefetch_related(
            "tags", "aist_project_versions",
        )
        pipeline_id = request.query_params.get("pipeline_id")
        if pipeline_id:
            pipeline = (
                get_authorized_aist_pipelines(Permissions.Product_View, user=request.user)
                .filter(id=pipeline_id)
                .first()
            )
            queryset = queryset.filter(test__aist_pipelines=pipeline) if pipeline else queryset.none()

        tags = _parse_tags(request)
        if tags:
            queryset = queryset.filter(tags__name__in=tags).distinct()
        severities = _parse_csv_values(request, "severity")
        if severities:
            queryset = queryset.filter(severity__in=severities).distinct()

        project_version = (request.query_params.get("project_version") or "").strip()
        if project_version:
            queryset = queryset.filter(aist_project_versions__version=project_version).distinct()

        file_path = (request.query_params.get("file") or "").strip()
        if file_path:
            queryset = queryset.filter(file_path__icontains=file_path)

        params = request.query_params.copy()
        if "tags" in params:
            params.pop("tags")
        if "pipeline_id" in params:
            params.pop("pipeline_id")
        if "project_version" in params:
            params.pop("project_version")
        if "file" in params:
            params.pop("file")
        if "severity" in params:
            params.pop("severity")
        ordering = params.get("ordering")
        if ordering and not params.get("o"):
            params["o"] = ordering

        filterset = ApiFindingFilter(data=params, queryset=queryset)
        queryset = filterset.qs

        paginator = LimitOffsetPagination()
        page = paginator.paginate_queryset(queryset, request)
        serializer = dojo_serializers.FindingSerializer(page, many=True, context={"request": request})
        payload = list(serializer.data)
        for row, finding in zip(payload, page, strict=True):
            row["project_version"] = _pick_project_version_label(finding)
            created = getattr(finding, "date", None) or getattr(finding, "created", None)
            row["created"] = created.isoformat() if created else None
        return paginator.get_paginated_response(payload)
