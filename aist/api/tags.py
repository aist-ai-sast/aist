from __future__ import annotations

from django.core.cache import cache
from dojo.authorization.roles_permissions import Permissions
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.query import AuthorizedQuerySetMixin, AuthorizedQuerysetSpec
from aist.queries import get_authorized_aist_projects, get_authorized_findings


class AvailableFindingTagsAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_projects,
        permission=Permissions.Product_View,
    )

    class QuerySerializer(serializers.Serializer):
        project_id = serializers.IntegerField(required=False)

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
        query_serializer = self.QuerySerializer(data=request.query_params)
        query_serializer.is_valid(raise_exception=True)
        project_id = query_serializer.validated_data.get("project_id")
        cache_key = f"aist_findings_tags_{request.user.id}_{project_id or 'all'}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response({"tags": cached})

        findings = self.get_authorized_queryset(
            getter=get_authorized_findings,
            permission=Permissions.Finding_View,
        )
        if project_id:
            project = self.get_authorized_queryset().filter(id=project_id).first()
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
