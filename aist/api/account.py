from __future__ import annotations

from django.contrib.auth import authenticate, get_user_model, login, logout, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from dojo.utils import get_system_setting
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from single_session.signals import remove_all_sessions

User = get_user_model()


def _is_profile_editable(user: User) -> bool:
    return bool(user.is_superuser or get_system_setting("enable_user_profile_editable"))


class AISTMeSerializer(serializers.ModelSerializer):
    can_edit_profile = serializers.SerializerMethodField()
    can_edit_username = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "username",
            "first_name",
            "last_name",
            "email",
            "can_edit_profile",
            "can_edit_username",
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

    def validate(self, attrs):
        request = self.context["request"]
        user = authenticate(
            request=request,
            username=attrs["username"],
            password=attrs["password"],
        )
        if user is None:
            msg = "Invalid username or password."
            raise AuthenticationFailed(msg)
        if not user.is_active:
            msg = "User account is disabled."
            raise AuthenticationFailed(msg)
        attrs["user"] = user
        return attrs


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
        login(request, serializer.validated_data["user"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class AISTAuthLogoutAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="Logout current session",
        responses={204: OpenApiResponse(description="Logged out")},
    )
    def post(self, request):
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AISTAuthLogoutAllAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="Logout all sessions for current user",
        responses={204: OpenApiResponse(description="Logged out from all sessions")},
    )
    def post(self, request):
        user = request.user
        remove_all_sessions(sender=type(user), user=user, request=request)
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)
