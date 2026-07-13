"""
REST API for organization member & per-project access management.

Views are intentionally thin: each resolves + authorizes the organization
(``Product_Type_Manage_Members``) and delegates every read/mutation to
``OrganizationMembershipService``. All business rules live in the service.
"""
from __future__ import annotations

from django.shortcuts import get_object_or_404
from dojo.authorization.roles_permissions import Permissions
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from aist.api.schema import AISTApiTag
from aist.members.service import OrganizationMembershipService
from aist.queries import get_authorized_aist_organizations

# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


class AISTProjectGrantSerializer(serializers.Serializer):
    project_id = serializers.IntegerField()
    product_id = serializers.IntegerField()
    project_name = serializers.CharField()
    role_id = serializers.IntegerField()
    role_name = serializers.CharField()


class AISTMemberSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.CharField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    is_active = serializers.BooleanField()
    role_id = serializers.IntegerField(allow_null=True)
    role_name = serializers.CharField()
    membership_type = serializers.CharField()
    has_token = serializers.BooleanField()
    token_count = serializers.IntegerField()
    project_grants = AISTProjectGrantSerializer(many=True)
    denied_project_ids = serializers.ListField(child=serializers.IntegerField())


class AISTProjectGrantWriteSerializer(serializers.Serializer):
    project_id = serializers.IntegerField()
    role_id = serializers.IntegerField()


class AISTMemberInviteSerializer(serializers.Serializer):
    email = serializers.EmailField()
    first_name = serializers.CharField(required=False, allow_blank=True, default="")
    last_name = serializers.CharField(required=False, allow_blank=True, default="")
    role_id = serializers.IntegerField(required=False, allow_null=True)
    project_grants = AISTProjectGrantWriteSerializer(many=True, required=False)


class AISTMemberRoleSerializer(serializers.Serializer):
    role_id = serializers.IntegerField()


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class _OrgMembersBaseAPI(APIView):

    """
    Resolves the managed organization and builds the membership service.

    ``get_authorized_aist_organizations(Product_Type_Manage_Members, ...)`` is
    the single management gate: an org the actor cannot manage yields 404.
    """

    permission_classes = [IsAuthenticated]

    def _service(self, request, org_id: int) -> OrganizationMembershipService:
        organization = get_object_or_404(
            get_authorized_aist_organizations(Permissions.Product_Type_Manage_Members, user=request.user),
            pk=org_id,
        )
        return OrganizationMembershipService(organization, request.user)


class AISTOrgMemberListCreateAPI(_OrgMembersBaseAPI):

    """Only ``post`` (invite, which sends an email) is throttled — ``get`` stays unlimited."""

    def get_throttles(self):
        if self.request.method != "POST":
            return []
        return [ScopedRateThrottle()]

    throttle_scope = "aist_invite_email"

    @extend_schema(
        tags=[AISTApiTag.MEMBERS.value],
        summary="List organization members",
        parameters=[OpenApiParameter("org_id", int, OpenApiParameter.PATH)],
        responses={200: AISTMemberSerializer(many=True)},
    )
    def get(self, request, org_id: int):
        service = self._service(request, org_id)
        return Response(AISTMemberSerializer(service.list_members(), many=True).data)

    @extend_schema(
        tags=[AISTApiTag.MEMBERS.value],
        summary="Invite / add an organization member",
        parameters=[OpenApiParameter("org_id", int, OpenApiParameter.PATH)],
        request=AISTMemberInviteSerializer,
        responses={201: OpenApiResponse(description="Member invited")},
    )
    def post(self, request, org_id: int):
        service = self._service(request, org_id)
        serializer = AISTMemberInviteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user, invite_status = service.invite_member(**serializer.validated_data)
        return Response(
            {
                "user_id": user.id,
                "username": user.username,
                "email": user.email,
                "invite_status": invite_status,
            },
            status=status.HTTP_201_CREATED,
        )


class AISTOrgMemberDetailAPI(_OrgMembersBaseAPI):

    @extend_schema(
        tags=[AISTApiTag.MEMBERS.value],
        summary="Change an organization member's role",
        request=AISTMemberRoleSerializer,
        responses={200: OpenApiResponse(description="Role updated")},
    )
    def patch(self, request, org_id: int, user_id: int):
        service = self._service(request, org_id)
        serializer = AISTMemberRoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        service.change_role(user_id=user_id, role_id=serializer.validated_data["role_id"])
        return Response({"ok": True})

    @extend_schema(
        tags=[AISTApiTag.MEMBERS.value],
        summary="Remove a member from the organization",
        responses={204: OpenApiResponse(description="Member removed")},
    )
    def delete(self, request, org_id: int, user_id: int):
        service = self._service(request, org_id)
        service.remove_member(user_id=user_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AISTOrgMemberResetPasswordAPI(_OrgMembersBaseAPI):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "aist_reset_password_email"

    @extend_schema(
        tags=[AISTApiTag.MEMBERS.value],
        summary="Email a member a set-password link",
        request=None,
        responses={200: OpenApiResponse(description="Reset email sent")},
    )
    def post(self, request, org_id: int, user_id: int):
        service = self._service(request, org_id)
        service.reset_password(user_id=user_id)
        return Response({"ok": True})


class AISTOrgMemberResetAccessAPI(_OrgMembersBaseAPI):

    """
    Explicit, deliberate way back to full org access for a restricted member.

    The only path that clears ``OrgMemberAccessScope.restricted`` — narrowing
    happens implicitly via grant/revoke, but broadening back to "sees every
    project" always requires this dedicated action, never a side effect of
    an emptied grant list.
    """

    @extend_schema(
        tags=[AISTApiTag.MEMBERS.value],
        summary="Reset a restricted member to full organization access",
        request=None,
        responses={200: OpenApiResponse(description="Member reset to full access")},
    )
    def post(self, request, org_id: int, user_id: int):
        service = self._service(request, org_id)
        service.reset_to_full_access(user_id=user_id)
        return Response({"ok": True})


class AISTOrgMemberProjectGrantListCreateAPI(_OrgMembersBaseAPI):

    @extend_schema(
        tags=[AISTApiTag.MEMBERS.value],
        summary="List a member's per-project grants",
        responses={200: AISTProjectGrantSerializer(many=True)},
    )
    def get(self, request, org_id: int, user_id: int):
        service = self._service(request, org_id)
        grants = service.list_project_grants(user_id)
        return Response(AISTProjectGrantSerializer(grants, many=True).data)

    @extend_schema(
        tags=[AISTApiTag.MEMBERS.value],
        summary="Grant a member access to a project",
        request=AISTProjectGrantWriteSerializer,
        responses={200: OpenApiResponse(description="Grant created")},
    )
    def post(self, request, org_id: int, user_id: int):
        service = self._service(request, org_id)
        serializer = AISTProjectGrantWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        service.grant_project(
            user_id=user_id,
            project_id=serializer.validated_data["project_id"],
            role_id=serializer.validated_data["role_id"],
        )
        return Response({"ok": True})


class AISTOrgMemberProjectGrantDetailAPI(_OrgMembersBaseAPI):

    @extend_schema(
        tags=[AISTApiTag.MEMBERS.value],
        summary="Revoke a member's access to a project",
        responses={204: OpenApiResponse(description="Grant revoked")},
    )
    def delete(self, request, org_id: int, user_id: int, project_id: int):
        service = self._service(request, org_id)
        service.revoke_project(user_id=user_id, project_id=project_id)
        return Response(status=status.HTTP_204_NO_CONTENT)
