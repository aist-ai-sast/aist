from __future__ import annotations

from typing import TYPE_CHECKING

from dojo.api_v2 import serializers as dojo_serializers
from dojo.authorization.roles_permissions import Permissions
from dojo.filters import ApiFindingFilter
from dojo.finding.queries import get_authorized_findings
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

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


class AISTFindingListAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs) -> Response:
        queryset = get_authorized_findings(Permissions.Finding_View, user=request.user)
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

        params = request.query_params.copy()
        if "tags" in params:
            params.pop("tags")
        if "pipeline_id" in params:
            params.pop("pipeline_id")
        ordering = params.get("ordering")
        if ordering and not params.get("o"):
            params["o"] = ordering

        filterset = ApiFindingFilter(data=params, queryset=queryset)
        queryset = filterset.qs

        paginator = LimitOffsetPagination()
        page = paginator.paginate_queryset(queryset, request)
        serializer = dojo_serializers.FindingSerializer(page, many=True, context={"request": request})
        return paginator.get_paginated_response(serializer.data)
