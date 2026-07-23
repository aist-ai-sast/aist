from __future__ import annotations

from dojo.models import Engagement, Test
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response

from aist.api.schema import AISTApiTag
from aist.authz import Action, AISTAPIView, ResourcePolicy


class AISTTestDetailSerializer(serializers.ModelSerializer):

    """Only the engagement linkage — all ``useTestEngagement`` (client-ui) reads."""

    engagement_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Test
        fields = ("id", "engagement", "engagement_id")


class AISTTestDetailAPI(AISTAPIView):
    # Read-only linkage endpoint; write action is a fail-secure default (no mutating handler).
    authz = ResourcePolicy(resource=Test, read=Action.TEST_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        tags=[AISTApiTag.FINDINGS.value],
        summary="Get a test's engagement linkage",
        responses={200: AISTTestDetailSerializer},
    )
    def get(self, request, test_id: int):
        test = self.resolve(id=test_id)
        return Response(AISTTestDetailSerializer(test).data)


class AISTEngagementDetailSerializer(serializers.ModelSerializer):

    """Only the product linkage — all ``useEngagementProduct`` (client-ui) reads."""

    product_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Engagement
        fields = ("id", "product", "product_id")


class AISTEngagementDetailAPI(AISTAPIView):
    authz = ResourcePolicy(resource=Engagement, read=Action.ENGAGEMENT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        tags=[AISTApiTag.FINDINGS.value],
        summary="Get an engagement's product linkage",
        responses={200: AISTEngagementDetailSerializer},
    )
    def get(self, request, engagement_id: int):
        engagement = self.resolve(id=engagement_id)
        return Response(AISTEngagementDetailSerializer(engagement).data)
