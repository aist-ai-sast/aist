"""
Self-service scoped API tokens + a superuser overview.

- ``/me/tokens/`` lets a user manage their OWN tokens (secret shown once).
- ``/admin/api-tokens/`` lets a superuser see WHO has tokens, never the secret.

Read-only vs read-write scope is enforced by ``AistTokenScopeMiddleware``. Each
token is bound to exactly one organization, and authorization is re-evaluated
from the owning user's current role inside that organization. A token can only
narrow capability, never widen it: creating a ``read_write`` token requires
write access in the selected organization.
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
from rest_framework.response import Response

from aist.api.schema import AISTApiTag
from aist.authz import INTERNAL_SERVICE, PUBLIC, AISTAPIView
from aist.models import AISTApiToken, ApiTokenScope, Organization
from aist.queries import get_visible_aist_organizations, user_has_write_capability

User = get_user_model()


def _tokens_for_request(request):
    queryset = AISTApiToken.objects.filter(user=request.user)
    if isinstance(request.auth, AISTApiToken):
        queryset = queryset.filter(organization=request.auth.organization)
    return queryset


class AISTApiTokenSerializer(serializers.Serializer):

    """Read serializer — deliberately has NO field for the secret or public_id."""

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField()
    scope = serializers.CharField()
    organization_id = serializers.IntegerField()
    organization_name = serializers.CharField(source="organization.name")
    last4 = serializers.CharField()
    created = serializers.DateTimeField()
    last_used_at = serializers.DateTimeField(allow_null=True)
    expires_at = serializers.DateTimeField(allow_null=True)


class AISTApiTokenCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    scope = serializers.ChoiceField(choices=ApiTokenScope.choices, default=ApiTokenScope.READ_ONLY)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)
    organization_id = serializers.PrimaryKeyRelatedField(
        source="organization",
        queryset=Organization.objects.none(),
    )

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request is not None and request.user.is_authenticated:
            fields["organization_id"].queryset = get_visible_aist_organizations(user=request.user)
        return fields

    def validate(self, attrs):
        user = self.context["request"].user
        organization = attrs["organization"]
        if attrs["scope"] == ApiTokenScope.READ_WRITE and not user_has_write_capability(
            user,
            organization=organization,
        ):
            msg = "You have no write access in this organization; create a read-only token instead."
            raise serializers.ValidationError({"scope": msg})
        if AISTApiToken.objects.filter(
            user=user,
            organization=organization,
            name=attrs["name"],
        ).exists():
            raise serializers.ValidationError({"name": "You already have a token with this name for this organization."})
        return attrs

    def validate_expires_at(self, value):
        if value is not None and value <= timezone.now():
            msg = "Expiry must be in the future."
            raise serializers.ValidationError(msg)
        return value


class AISTMeTokenListCreateAPI(AISTAPIView):
    authz = PUBLIC
    # The create response returns the secret exactly once; opt out of response
    # masking so it is not blanked to ****. (The list response carries no secret.)
    disable_response_masking = True

    @extend_schema(
        tags=[AISTApiTag.TOKENS.value],
        summary="List my API tokens",
        responses={200: AISTApiTokenSerializer(many=True)},
    )
    def get(self, request):
        tokens = _tokens_for_request(request).order_by("-created")
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
                organization=serializer.validated_data["organization"],
                name=serializer.validated_data["name"],
                scope=serializer.validated_data["scope"],
                expires_at=serializer.validated_data.get("expires_at"),
            )
        except IntegrityError as exc:
            msg = "You already have a token with this name for this organization."
            raise serializers.ValidationError({"name": msg}) from exc
        payload = AISTApiTokenSerializer(token).data
        payload["token"] = raw  # the ONLY time the secret is ever returned
        return Response(payload, status=status.HTTP_201_CREATED)


class AISTMeTokenDetailAPI(AISTAPIView):
    authz = PUBLIC

    @extend_schema(
        tags=[AISTApiTag.TOKENS.value],
        summary="Revoke one of my API tokens",
        responses={204: OpenApiResponse(description="Token revoked")},
    )
    def delete(self, request, token_id: int):
        token = get_object_or_404(_tokens_for_request(request), pk=token_id)
        token.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AISTAdminApiTokenListAPI(AISTAPIView):

    """Superuser overview of which users hold tokens. Never exposes a secret."""

    authz = INTERNAL_SERVICE
    # Exclude ScopedTokenAuthentication: this enumeration is reachable only via a
    # UI session or the internal service token, never via any scoped PAT.
    authentication_classes = [SessionAuthentication, BasicAuthentication, TokenAuthentication]

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
            .select_related("user", "organization")
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
                    "organization_id": token.organization_id,
                    "organization_name": token.organization.name,
                    "created": token.created,
                    "last_used_at": token.last_used_at,
                    "expires_at": token.expires_at,
                },
            )
        return Response(list(by_user.values()))
