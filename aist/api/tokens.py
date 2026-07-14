"""
Self-service scoped API tokens + a superuser overview.

- ``/me/tokens/`` lets a user manage their OWN tokens (secret shown once).
- ``/admin/api-tokens/`` lets a superuser see WHO has tokens, never the secret.

Read-only vs read-write scope is enforced by ``AistTokenScopeMiddleware``. The
org-scoping half of a token's effective permission is unchanged: it comes from
the owning user's role via ``aist/queries.py``. A token can only narrow
capability, never widen it — enforced up front too: creating a ``read_write``
token requires the user to already hold write access somewhere (see
``AISTApiTokenCreateSerializer.validate_scope`` /
``aist.queries.user_has_write_capability``), so a Reader can't even mint a
token whose scope implies a capability they don't have.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.authentication import (
    BasicAuthentication,
    SessionAuthentication,
    TokenAuthentication,
)
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.schema import AISTApiTag
from aist.models import AISTApiToken, ApiTokenScope
from aist.queries import user_has_write_capability

User = get_user_model()


class AISTApiTokenSerializer(serializers.Serializer):

    """Read serializer — deliberately has NO field for the secret or public_id."""

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField()
    scope = serializers.CharField()
    last4 = serializers.CharField()
    created = serializers.DateTimeField()
    last_used_at = serializers.DateTimeField(allow_null=True)
    expires_at = serializers.DateTimeField(allow_null=True)


class AISTApiTokenCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    scope = serializers.ChoiceField(choices=ApiTokenScope.choices, default=ApiTokenScope.READ_ONLY)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)

    def validate_name(self, value: str) -> str:
        user = self.context["request"].user
        if AISTApiToken.objects.filter(user=user, name=value).exists():
            msg = "You already have a token with this name."
            raise serializers.ValidationError(msg)
        return value

    def validate_expires_at(self, value):
        if value is not None and value <= timezone.now():
            msg = "Expiry must be in the future."
            raise serializers.ValidationError(msg)
        return value

    def validate_scope(self, value: str) -> str:
        user = self.context["request"].user
        if value == ApiTokenScope.READ_WRITE and not user_has_write_capability(user):
            msg = "You have no write access anywhere, so a read/write token would be useless — create a read-only token instead."
            raise serializers.ValidationError(msg)
        return value


class AISTMeTokenListCreateAPI(APIView):
    permission_classes = [IsAuthenticated]
    # The create response returns the secret exactly once; opt out of response
    # masking so it is not blanked to ****. (The list response carries no secret.)
    disable_response_masking = True

    @extend_schema(
        tags=[AISTApiTag.TOKENS.value],
        summary="List my API tokens",
        responses={200: AISTApiTokenSerializer(many=True)},
    )
    def get(self, request):
        tokens = AISTApiToken.objects.filter(user=request.user).order_by("-created")
        return Response(AISTApiTokenSerializer(tokens, many=True).data)

    @extend_schema(
        tags=[AISTApiTag.TOKENS.value],
        summary="Create an API token (secret returned once)",
        request=AISTApiTokenCreateSerializer,
        responses={201: OpenApiResponse(description="Token created; 'token' field holds the secret, shown once")},
    )
    def post(self, request):
        serializer = AISTApiTokenCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        try:
            # validate_name's check-then-create isn't race-proof; the DB's
            # unique_together=(user, name) is the real guard for two
            # concurrent creates of the same name. Without this, that race
            # surfaces as an unhandled 500 instead of the same 400 a
            # sequential duplicate gets.
            token, raw = AISTApiToken.issue(
                user=request.user,
                name=serializer.validated_data["name"],
                scope=serializer.validated_data["scope"],
                expires_at=serializer.validated_data.get("expires_at"),
            )
        except IntegrityError as exc:
            msg = "You already have a token with this name."
            raise serializers.ValidationError({"name": msg}) from exc
        payload = AISTApiTokenSerializer(token).data
        payload["token"] = raw  # the ONLY time the secret is ever returned
        return Response(payload, status=status.HTTP_201_CREATED)


class AISTMeTokenDetailAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[AISTApiTag.TOKENS.value],
        summary="Revoke one of my API tokens",
        responses={204: OpenApiResponse(description="Token revoked")},
    )
    def delete(self, request, token_id: int):
        token = get_object_or_404(AISTApiToken, pk=token_id, user=request.user)
        token.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AISTAdminApiTokenListAPI(APIView):

    """Superuser overview of which users hold tokens. Never exposes a secret."""

    # Exclude ScopedTokenAuthentication: this enumeration is reachable only via a
    # UI session or the internal service token, never via any scoped PAT.
    authentication_classes = [SessionAuthentication, BasicAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[AISTApiTag.TOKENS.value],
        summary="List users that hold API tokens (superuser only)",
        responses={200: OpenApiResponse(description="Users and their token metadata")},
    )
    def get(self, request):
        if not request.user.is_superuser:
            msg = "Superuser access required."
            raise PermissionDenied(msg)

        by_user: dict[int, dict] = {}
        tokens = (
            AISTApiToken.objects
            .select_related("user")
            .order_by("user__username", "-created")
        )
        for token in tokens:
            entry = by_user.setdefault(
                token.user_id,
                {"user_id": token.user_id, "username": token.user.username, "token_count": 0, "tokens": []},
            )
            entry["token_count"] += 1
            entry["tokens"].append(
                {
                    "name": token.name,
                    "scope": token.scope,
                    "created": token.created,
                    "last_used_at": token.last_used_at,
                    "expires_at": token.expires_at,
                },
            )
        return Response(list(by_user.values()))
