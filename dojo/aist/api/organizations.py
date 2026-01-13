from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import generics, serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response  # noqa: TC002

from dojo.aist.models import Organization


class AISTOrganizationSerializer(serializers.ModelSerializer):

    """Serializer for Organization model used in AIST UI."""

    class Meta:
        model = Organization
        fields = ("id", "name", "created", "updated")


class OrganizationCreateAPI(generics.CreateAPIView):

    """Create a new Organization that can be assigned to AISTProject instances."""

    permission_classes = [IsAuthenticated]
    serializer_class = AISTOrganizationSerializer
    queryset = Organization.objects.all()

    @extend_schema(
        tags=["aist"],
        summary="Create organization",
        description="Creates a new organization that can be used to group AIST projects.",
        request=AISTOrganizationSerializer,
        responses={201: OpenApiResponse(AISTOrganizationSerializer, description="Organization created")},
    )
    def post(self, request, *args, **kwargs) -> Response:
        # Use generic CreateAPIView logic for validation + object creation
        return super().post(request, *args, **kwargs)
