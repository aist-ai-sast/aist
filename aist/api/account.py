from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth import authenticate, get_user_model, login, logout, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.contrib.auth.tokens import default_token_generator
from django.utils.decorators import method_decorator
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from django.views.decorators.csrf import csrf_protect
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product_Type_Group, Product_Type_Member, UserContactInfo
from dojo.utils import get_system_setting
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from single_session.signals import remove_all_sessions

from aist.api.schema import AISTApiTag
from aist.authz import PUBLIC, AISTAPIView
from aist.queries import get_visible_aist_organizations, user_has_write_capability
from aist.roles import role_rank

User = get_user_model()


def _is_profile_editable(user: User) -> bool:
    return bool(user.is_superuser or get_system_setting("enable_user_profile_editable"))


@dataclass(slots=True)
class OrganizationMembership:
    organization_id: int
    organization_name: str
    role_id: int | None
    role_name: str

    @property
    def can_write_findings(self) -> bool:
        return self.role_name == "Superuser" or role_rank(self.role_id) >= role_rank(Roles.Writer.value)

    @property
    def can_operate_projects(self) -> bool:
        return self.role_name == "Superuser" or role_rank(self.role_id) >= role_rank(Roles.Maintainer.value)

    @property
    def can_manage_access(self) -> bool:
        return self.can_operate_projects

    @property
    def can_grant_owner(self) -> bool:
        return self.role_name == "Superuser" or role_rank(self.role_id) >= role_rank(Roles.Owner.value)


class _MembershipAccumulator:

    """
    Resolves the single best (highest-role) membership per organization.

    A user may reach an organization through several grant sources
    (org-wide role, group role, per-project role). This object owns the
    "keep the highest-ranked grant per org" rule so the resolution logic
    lives in one place instead of being repeated per source.
    """

    def __init__(self, organizations: list) -> None:
        self._org_by_id = {org.id: org for org in organizations}
        self._best: dict[int, OrganizationMembership] = {}

    @property
    def organization_ids(self):
        return self._org_by_id.keys()

    def add_grant_rows(self, rows, organization_id_field: str) -> None:
        """Merge ``.values(...)`` rows carrying ``role_id`` and ``role__name``."""
        for row in rows:
            org = self._org_by_id.get(row[organization_id_field])
            if org is None:
                continue
            self._offer(
                OrganizationMembership(
                    organization_id=org.id,
                    organization_name=org.name,
                    role_id=row["role_id"],
                    role_name=row["role__name"] or "Reader",
                ),
            )

    def fill_missing_with_reader(self) -> None:
        """
        Default any still-unresolved visible org to Reader.

        This covers visibility granted through a channel that carries no explicit
        membership row (e.g. a global Product_View permission).
        """
        for org in self._org_by_id.values():
            if org.id not in self._best:
                self._best[org.id] = OrganizationMembership(
                    organization_id=org.id,
                    organization_name=org.name,
                    role_id=Roles.Reader.value,
                    role_name="Reader",
                )

    def result(self) -> list[OrganizationMembership]:
        return sorted(self._best.values(), key=lambda item: item.organization_name.lower())

    def _offer(self, candidate: OrganizationMembership) -> None:
        current = self._best.get(candidate.organization_id)
        if current is None or role_rank(candidate.role_id) > role_rank(current.role_id):
            self._best[candidate.organization_id] = candidate


def _get_organization_memberships(user: User) -> list[OrganizationMembership]:
    authorized_orgs = list(
        get_visible_aist_organizations(user=user)
        .order_by("name")
        .only("id", "name", "product_type_id"),
    )
    if user.is_superuser:
        return [
            OrganizationMembership(
                organization_id=org.id,
                organization_name=org.name,
                role_id=None,
                role_name="Superuser",
            )
            for org in authorized_orgs
        ]
    if not authorized_orgs:
        return []

    accumulator = _MembershipAccumulator(authorized_orgs)
    org_ids = accumulator.organization_ids

    accumulator.add_grant_rows(
        Product_Type_Member.objects.filter(user=user, product_type__aist_organization__in=org_ids)
        .values("product_type__aist_organization", "role_id", "role__name")
        .distinct(),
        "product_type__aist_organization",
    )
    accumulator.add_grant_rows(
        Product_Type_Group.objects.filter(group__users=user, product_type__aist_organization__in=org_ids)
        .values("product_type__aist_organization", "role_id", "role__name")
        .distinct(),
        "product_type__aist_organization",
    )
    accumulator.fill_missing_with_reader()
    return accumulator.result()


class AISTOrganizationMembershipSerializer(serializers.Serializer):
    organization_id = serializers.IntegerField()
    organization_name = serializers.CharField()
    role_id = serializers.IntegerField(allow_null=True)
    role_name = serializers.CharField()
    can_write_findings = serializers.BooleanField()
    can_operate_projects = serializers.BooleanField()
    can_manage_access = serializers.BooleanField()
    can_grant_owner = serializers.BooleanField()


class AISTMeSerializer(serializers.ModelSerializer):
    can_edit_profile = serializers.SerializerMethodField()
    can_edit_username = serializers.SerializerMethodField()
    organization_memberships = serializers.SerializerMethodField()
    can_create_write_token = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "username",
            "first_name",
            "last_name",
            "email",
            "is_superuser",
            "can_edit_profile",
            "can_edit_username",
            "organization_memberships",
            "can_create_write_token",
        )
        read_only_fields = ("is_superuser",)
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

    def get_can_create_write_token(self, obj) -> bool:
        # Same check the backend enforces in AISTApiTokenCreateSerializer.validate_scope
        # (aist.queries.user_has_write_capability) — surfaced here purely so the UI can
        # disable the "read/write" token option up front instead of the user hitting a 400.
        return user_has_write_capability(obj)

    def validate(self, attrs):
        user = self.instance or self.context["request"].user
        if not _is_profile_editable(user):
            msg = "Profile editing is disabled by policy."
            raise serializers.ValidationError(msg)
        if "username" in attrs and not self.get_can_edit_username(user):
            msg = "Username editing is disabled by policy."
            raise serializers.ValidationError({"username": msg})
        new_email = attrs.get("email")
        if new_email and User.objects.filter(email__iexact=new_email).exclude(pk=user.pk).exists():
            msg = "Another account already uses this email."
            raise serializers.ValidationError({"email": msg})
        return attrs


# Django's PasswordChangeForm/SetPasswordForm key their errors by FORM field
# names, which never match either serializer's own field names below — passing
# form.errors straight through produced a 400 body the frontend's error
# extractor couldn't read at all (falling back to a generic "Request failed").
_FORM_FIELD_TO_SERIALIZER_FIELD = {
    "old_password": "current_password",
    "new_password1": "new_password",
    "new_password2": "new_password_confirm",
}


def _password_form_errors(form) -> dict[str, list[str]]:
    remapped: dict[str, list[str]] = {}
    for field, messages in form.errors.items():
        key = _FORM_FIELD_TO_SERIALIZER_FIELD.get(field, field)
        remapped.setdefault(key, []).extend(messages)
    return remapped


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
            raise serializers.ValidationError(_password_form_errors(form))
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


class AISTSetPasswordSerializer(serializers.Serializer):

    """Validate a reset/invite token and set the user's chosen password."""

    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password_confirm = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        if attrs["new_password"] != attrs["new_password_confirm"]:
            msg = "New passwords do not match."
            raise serializers.ValidationError({"new_password_confirm": msg})
        user = self._user_from_uid(attrs["uid"])
        if user is None or not default_token_generator.check_token(user, attrs["token"]):
            msg = "This link is invalid or has expired."
            raise serializers.ValidationError({"token": msg})
        form = SetPasswordForm(
            user=user,
            data={"new_password1": attrs["new_password"], "new_password2": attrs["new_password_confirm"]},
        )
        if not form.is_valid():
            raise serializers.ValidationError(_password_form_errors(form))
        self.context["set_password_form"] = form
        self.context["target_user"] = user
        return attrs

    @staticmethod
    def _user_from_uid(uid: str):
        try:
            pk = force_str(urlsafe_base64_decode(uid))
            return User.objects.get(pk=pk)
        except (User.DoesNotExist, ValueError, TypeError, OverflowError):
            return None

    def save(self, **kwargs):
        user = self.context["target_user"]
        self.context["set_password_form"].save()
        # The user has now set their own password; clear any forced-reset flag.
        contact_info, _ = UserContactInfo.objects.get_or_create(user=user)
        if contact_info.force_password_reset:
            contact_info.force_password_reset = False
            contact_info.save(update_fields=["force_password_reset"])
        return user


class AISTMeAPI(AISTAPIView):
    authz = PUBLIC

    @extend_schema(
        tags=[AISTApiTag.PROFILE.value],
        summary="Current user account",
        responses={200: AISTMeSerializer},
    )
    def get(self, request):
        serializer = AISTMeSerializer(request.user, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        tags=[AISTApiTag.PROFILE.value],
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


class AISTMeChangePasswordAPI(AISTAPIView):
    authz = PUBLIC
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "aist_change_password"

    @extend_schema(
        tags=[AISTApiTag.PROFILE.value],
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
class AISTAuthLoginAPI(AISTAPIView):
    authz = PUBLIC
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "aist_auth_login"

    @extend_schema(
        tags=[AISTApiTag.AUTH.value],
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


@method_decorator(csrf_protect, name="dispatch")
class AISTSetPasswordAPI(AISTAPIView):

    """Anonymous endpoint: set a password from an emailed invite/reset link."""

    authz = PUBLIC
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "aist_auth_set_password"

    @extend_schema(
        tags=[AISTApiTag.AUTH.value],
        summary="Set password from an invite/reset link",
        request=AISTSetPasswordSerializer,
        responses={204: OpenApiResponse(description="Password set")},
    )
    def post(self, request):
        serializer = AISTSetPasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AISTAuthLogoutAPI(AISTAPIView):
    authz = PUBLIC

    @extend_schema(
        tags=[AISTApiTag.AUTH.value],
        summary="Logout current session",
        request=None,
        responses={204: OpenApiResponse(description="Logged out")},
    )
    def post(self, request):
        raw_request = getattr(request, "_request", request)
        if hasattr(raw_request, "session"):
            logout(raw_request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AISTAuthLogoutAllAPI(AISTAPIView):
    authz = PUBLIC

    @extend_schema(
        tags=[AISTApiTag.AUTH.value],
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
