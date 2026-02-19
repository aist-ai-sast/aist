from __future__ import annotations

from django.core.cache import cache
from dojo.authorization.roles_permissions import Permissions
from dojo.finding.queries import get_authorized_findings
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.models import AISTProject


class AvailableFindingTagsAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="List available finding tags",
        parameters=[OpenApiParameter(name="project_id", required=False, type=int)],
        responses={
            200: inline_serializer(
                name="AvailableFindingTagsResponse",
                fields={"tags": serializers.ListField(child=serializers.CharField())},
            ),
        },
    )
    def get(self, request):
        project_id = request.query_params.get("project_id")
        cache_key = f"aist_findings_tags_{request.user.id}_{project_id or 'all'}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response({"tags": cached})

        findings = get_authorized_findings(Permissions.Finding_View, user=request.user)
        if project_id:
            project = AISTProject.objects.filter(id=project_id).first()
            findings = findings.filter(test__engagement__product_id=project.product_id) if project else findings.none()
        tags = (
            findings.values_list("tags__name", flat=True)
            .exclude(tags__name__isnull=True)
            .exclude(tags__name__exact="")
            .distinct()
            .order_by("tags__name")
        )
        result = list(tags)
        cache.set(cache_key, result, 300)
        return Response({"tags": result})
