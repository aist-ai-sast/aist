from __future__ import annotations

from dojo.authorization.roles_permissions import Permissions
from dojo.models import Engagement, Test
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.query import AuthorizedQuerySetMixin, AuthorizedQuerysetSpec
from aist.api.schema import AISTApiTag
from aist.queries import get_authorized_engagements, get_authorized_tests


class AISTTestDetailSerializer(serializers.ModelSerializer):

    """Only the engagement linkage — all ``useTestEngagement`` (client-ui) reads."""

    engagement_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Test
        fields = ("id", "engagement", "engagement_id")


class AISTTestDetailAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_tests,
        permission=Permissions.Test_View,
    )

    @extend_schema(
        tags=[AISTApiTag.FINDINGS.value],
        summary="Get a test's engagement linkage",
        responses={200: AISTTestDetailSerializer},
    )
    def get(self, request, test_id: int):
        test = self.get_authorized_object(id=test_id)
        return Response(AISTTestDetailSerializer(test).data)


class AISTEngagementDetailSerializer(serializers.ModelSerializer):

    """Only the product linkage — all ``useEngagementProduct`` (client-ui) reads."""

    product_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Engagement
        fields = ("id", "product", "product_id")


class AISTEngagementDetailAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_engagements,
        permission=Permissions.Engagement_View,
    )

    @extend_schema(
        tags=[AISTApiTag.FINDINGS.value],
        summary="Get an engagement's product linkage",
        responses={200: AISTEngagementDetailSerializer},
    )
    def get(self, request, engagement_id: int):
        engagement = self.get_authorized_object(id=engagement_id)
        return Response(AISTEngagementDetailSerializer(engagement).data)
