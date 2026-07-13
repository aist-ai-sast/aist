from __future__ import annotations

from dojo.authorization.authorization import user_has_global_permission_or_403
from dojo.authorization.roles_permissions import Permissions
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import generics, serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response  # noqa: TC002

from aist.api.schema import AISTApiTag
from aist.models import Organization
from aist.queries import get_authorized_aist_organizations, get_visible_aist_organizations


class AISTOrganizationSerializer(serializers.ModelSerializer):

    """Serializer for Organization model used in AIST UI."""

    product_type_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Organization
        fields = ("id", "name", "description", "metadata", "product_type_id", "created", "updated")

    def create(self, validated_data):
        request = self.context.get("request")
        # Product_Type_Add is required unconditionally, even when an existing
        # Product_Type is being reused by name — skipping it in that branch let
        # any authenticated user create an Organization (and permanently claim
        # its unique name) with zero permission check, just by naming it after
        # any pre-existing Product_Type.
        if request:
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
        manage = self.request.query_params.get("manage", "").lower() == "true"
        if manage:
            return get_authorized_aist_organizations(
                Permissions.Product_Type_Manage_Members, user=self.request.user,
            ).order_by("name")
        # Listing (non-manage) drives navigation, so it must include restricted
        # members who only hold per-project grants in the organization.
        return get_visible_aist_organizations(user=self.request.user).order_by("name")

    @extend_schema(
        tags=[AISTApiTag.ORGANIZATIONS.value],
        summary="Create organization",
        description="Creates a new organization that can be used to group AIST projects.",
        request=AISTOrganizationSerializer,
        responses={201: OpenApiResponse(AISTOrganizationSerializer, description="Organization created")},
    )
    def post(self, request, *args, **kwargs) -> Response:
        # Use generic CreateAPIView logic for validation + object creation
        return super().post(request, *args, **kwargs)

    @extend_schema(
        tags=[AISTApiTag.ORGANIZATIONS.value],
        summary="List organizations",
        description="Returns organizations available to the current user.",
        responses={200: OpenApiResponse(AISTOrganizationSerializer(many=True), description="Organizations list")},
    )
    def get(self, request, *args, **kwargs) -> Response:
        return super().get(request, *args, **kwargs)
