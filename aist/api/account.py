from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth import authenticate, get_user_model, login, logout, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product_Type_Member
from dojo.utils import get_system_setting
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from single_session.signals import remove_all_sessions

from aist.models import Organization

User = get_user_model()


def _is_profile_editable(user: User) -> bool:
    return bool(user.is_superuser or get_system_setting("enable_user_profile_editable"))


@dataclass(slots=True)
class OrganizationMembership:
    organization_id: int
    organization_name: str
    role_id: int | None
    role_name: str


def _role_rank(role_id: int | None) -> int:
    ranks = {
        Roles.Reader.value: 0,
        Roles.API_Importer.value: 1,
        Roles.Writer.value: 2,
        Roles.Maintainer.value: 3,
        Roles.Owner.value: 4,
    }
    return ranks.get(role_id, -1)


def _get_organization_memberships(user: User) -> list[OrganizationMembership]:
    if user.is_superuser:
        return [
            OrganizationMembership(
                organization_id=org.id,
                organization_name=org.name,
                role_id=None,
                role_name="Superuser",
            )
            for org in Organization.objects.order_by("name").only("id", "name")
        ]

    rows = (
        Product_Type_Member.objects.filter(
            user=user,
            product_type__aist_organization__isnull=False,
        )
        .values(
            "product_type__aist_organization",
            "product_type__aist_organization__name",
            "role_id",
            "role__name",
        )
        .distinct()
    )
    by_org: dict[int, OrganizationMembership] = {}
    for row in rows:
        organization_id = row["product_type__aist_organization"]
        if not organization_id:
            continue
        candidate = OrganizationMembership(
            organization_id=organization_id,
            organization_name=row["product_type__aist_organization__name"] or "",
            role_id=row["role_id"],
            role_name=row["role__name"] or "Reader",
        )
        current = by_org.get(organization_id)
        if current is None or _role_rank(candidate.role_id) > _role_rank(current.role_id):
            by_org[organization_id] = candidate
    return sorted(by_org.values(), key=lambda item: item.organization_name.lower())


class AISTOrganizationMembershipSerializer(serializers.Serializer):
    organization_id = serializers.IntegerField()
    organization_name = serializers.CharField()
    role_id = serializers.IntegerField(allow_null=True)
    role_name = serializers.CharField()


class AISTMeSerializer(serializers.ModelSerializer):
    can_edit_profile = serializers.SerializerMethodField()
    can_edit_username = serializers.SerializerMethodField()
    organization_memberships = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "username",
            "first_name",
            "last_name",
            "email",
            "can_edit_profile",
            "can_edit_username",
            "organization_memberships",
        )
        extra_kwargs = {
            "username": {"required": False},
            "first_name": {"required": False, "allow_blank": True},
            "last_name": {"required": False, "allow_blank": True},
            "email": {"required": False, "allow_blank": True},
        }

    def get_can_edit_profile(self, obj) -> bool:
        return _is_profile_editable(obj)

    def get_can_edit_username(self, obj) -> bool:
        return _is_profile_editable(obj)

    def get_organization_memberships(self, obj) -> list[dict]:
        memberships = _get_organization_memberships(obj)
        return AISTOrganizationMembershipSerializer(memberships, many=True).data

    def validate(self, attrs):
        user = self.instance or self.context["request"].user
        if not _is_profile_editable(user):
            msg = "Profile editing is disabled by policy."
            raise serializers.ValidationError(msg)
        if "username" in attrs and not self.get_can_edit_username(user):
            msg = "Username editing is disabled by policy."
            raise serializers.ValidationError({"username": msg})
        return attrs


class AISTChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password_confirm = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        if attrs["new_password"] != attrs["new_password_confirm"]:
            msg = "New passwords do not match."
            raise serializers.ValidationError({"new_password_confirm": msg})

        request = self.context["request"]
        form = PasswordChangeForm(
            user=request.user,
            data={
                "old_password": attrs["current_password"],
                "new_password1": attrs["new_password"],
                "new_password2": attrs["new_password_confirm"],
            },
        )
        if not form.is_valid():
            raise serializers.ValidationError(form.errors)
        self.context["password_form"] = form
        return attrs

    def save(self, **kwargs):
        request = self.context["request"]
        form: PasswordChangeForm = self.context["password_form"]
        user = form.save()
        update_session_auth_hash(request, user)
        return user


class AISTAuthLoginSerializer(serializers.Serializer):
    username = serializers.CharField(trim_whitespace=True)
    password = serializers.CharField(trim_whitespace=False, write_only=True)


class AISTMeAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="Current user account",
        responses={200: AISTMeSerializer},
    )
    def get(self, request):
        serializer = AISTMeSerializer(request.user, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        tags=["aist"],
        summary="Update current user account",
        request=AISTMeSerializer,
        responses={200: AISTMeSerializer},
    )
    def patch(self, request):
        serializer = AISTMeSerializer(
            request.user,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)


class AISTMeChangePasswordAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="Change current user password",
        request=AISTChangePasswordSerializer,
        responses={200: OpenApiResponse(description="Password changed")},
    )
    def post(self, request):
        serializer = AISTChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"ok": True}, status=status.HTTP_200_OK)


@method_decorator(csrf_protect, name="dispatch")
class AISTAuthLoginAPI(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "aist_auth_login"

    @extend_schema(
        tags=["aist"],
        summary="Login current client session",
        request=AISTAuthLoginSerializer,
        responses={204: OpenApiResponse(description="Logged in")},
    )
    def post(self, request):
        serializer = AISTAuthLoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = authenticate(
            request=request,
            username=serializer.validated_data["username"],
            password=serializer.validated_data["password"],
        )
        if user is None:
            return Response({"detail": "Invalid username or password."}, status=status.HTTP_401_UNAUTHORIZED)
        if not user.is_active:
            return Response({"detail": "User account is disabled."}, status=status.HTTP_401_UNAUTHORIZED)
        login(request, user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AISTAuthLogoutAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="Logout current session",
        request=None,
        responses={204: OpenApiResponse(description="Logged out")},
    )
    def post(self, request):
        raw_request = getattr(request, "_request", request)
        if hasattr(raw_request, "session"):
            logout(raw_request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AISTAuthLogoutAllAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="Logout all sessions for current user",
        request=None,
        responses={204: OpenApiResponse(description="Logged out from all sessions")},
    )
    def post(self, request):
        user = request.user
        raw_request = getattr(request, "_request", request)
        remove_all_sessions(sender=type(user), user=user, request=raw_request)
        if hasattr(raw_request, "session"):
            logout(raw_request)
        return Response(status=status.HTTP_204_NO_CONTENT)
