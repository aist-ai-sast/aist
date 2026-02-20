from __future__ import annotations

from dojo.authorization.authorization import user_has_global_permission_or_403
from dojo.authorization.roles_permissions import Permissions
from dojo.models import Product_Type
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import generics, serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response  # noqa: TC002

from aist.models import Organization
from aist.queries import get_authorized_aist_organizations


class AISTOrganizationSerializer(serializers.ModelSerializer):

    """Serializer for Organization model used in AIST UI."""

    product_type_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Organization
        fields = ("id", "name", "description", "metadata", "product_type_id", "created", "updated")

    def create(self, validated_data):
        request = self.context.get("request")
        organization_name = validated_data["name"]
        if request and not Product_Type.objects.filter(name=organization_name).exists():
            user_has_global_permission_or_403(request.user, Permissions.Product_Type_Add)
        organization = super().create(validated_data)
        organization.ensure_product_type()
        organization.refresh_from_db()
        return organization


class OrganizationCreateAPI(generics.ListCreateAPIView):

    """Create a new Organization that can be assigned to AISTProject instances."""

    permission_classes = [IsAuthenticated]
    serializer_class = AISTOrganizationSerializer
    queryset = Organization.objects.all()

    def get_queryset(self):
        return get_authorized_aist_organizations(Permissions.Product_View, user=self.request.user).order_by("name")

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

    @extend_schema(
        tags=["aist"],
        summary="List organizations",
        description="Returns organizations available to the current user.",
        responses={200: OpenApiResponse(AISTOrganizationSerializer(many=True), description="Organizations list")},
    )
    def get(self, request, *args, **kwargs) -> Response:
        return super().get(request, *args, **kwargs)
