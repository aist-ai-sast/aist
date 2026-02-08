from __future__ import annotations

from typing import List

from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from dojo.authorization.roles_permissions import Permissions
from dojo.filters import ApiFindingFilter
from dojo.finding.queries import get_authorized_findings
from dojo.api_v2 import serializers as dojo_serializers


def _parse_tags(request) -> List[str]:
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

        tags = _parse_tags(request)
        if tags:
            queryset = queryset.filter(tags__name__in=tags).distinct()

        params = request.query_params.copy()
        if "tags" in params:
            params.pop("tags")
        ordering = params.get("ordering")
        if ordering and not params.get("o"):
            params["o"] = ordering

        filterset = ApiFindingFilter(data=params, queryset=queryset)
        queryset = filterset.qs

        paginator = LimitOffsetPagination()
        page = paginator.paginate_queryset(queryset, request)
        serializer = dojo_serializers.FindingSerializer(page, many=True, context={"request": request})
        return paginator.get_paginated_response(serializer.data)
