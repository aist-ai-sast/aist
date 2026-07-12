"""
Authentication for AIST personal access tokens (``Authorization: Bearer aistpat_...``).

Secret generation, hashing and verification live on the ``AISTApiToken`` model
(RAII) — this module only wires the model into DRF and exposes small helpers for
the admin-guard middleware. Tokens authenticate ONLY on the AIST API; the
``AistAdminGuardMiddleware`` rejects any ``aistpat_`` token on the vendor admin
API, so a scoped token can never reach it regardless of its owner.
"""
from __future__ import annotations

from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from aist.models import AIST_TOKEN_PREFIX, AISTApiToken

_BEARER_KEYWORD = b"bearer"
# Avoid a DB write on every request: only refresh last_used_at when it is stale.
_LAST_USED_THROTTLE_SECONDS = 60


def header_carries_scoped_token(request) -> bool:
    """
    True when the Authorization header is a ``Bearer aistpat_...`` token.

    Lets the admin-guard middleware reject scoped tokens on the vendor admin API
    without a DB lookup — the mere presence of an ``aistpat_`` bearer token is
    enough to deny, valid or not.
    """
    header = get_authorization_header(request).split()
    return (
        len(header) == 2
        and header[0].lower() == _BEARER_KEYWORD
        and header[1].startswith(AIST_TOKEN_PREFIX.encode())
    )


def resolve_scoped_token(request) -> AISTApiToken | None:
    """Return the valid ``AISTApiToken`` a request carries, or ``None`` (non-raising)."""
    parsed = _parse_header(request)
    if parsed is None:
        return None
    public_id, secret = parsed
    token = AISTApiToken.objects.filter(public_id=public_id).select_related("user").first()
    if token is None or not token.verify_secret(secret):
        return None
    return token


def _parse_header(request) -> tuple[str, str] | None:
    header = get_authorization_header(request).split()
    if len(header) != 2 or header[0].lower() != _BEARER_KEYWORD:
        return None
    return AISTApiToken.parse_raw(header[1].decode(errors="ignore"))


class ScopedTokenAuthentication(BaseAuthentication):

    """
    Authenticates ``Authorization: Bearer aistpat_<public_id>_<secret>``.

    Returns ``None`` (declines) for any header that is not one of our tokens, so
    the stock Session / Basic / Token authenticators still run — the internal
    superuser service token keeps working. ``request.auth`` is set to the
    ``AISTApiToken`` instance so scope enforcement can read it.
    """

    def authenticate(self, request):
        parsed = _parse_header(request)
        if parsed is None:
            return None  # not our header — let other authenticators try
        public_id, secret = parsed
        token = AISTApiToken.objects.filter(public_id=public_id).select_related("user").first()
        if token is None or not token.verify_secret(secret):
            msg = "Invalid API token."
            raise AuthenticationFailed(msg)
        if not token.is_usable:
            msg = "API token has expired or been revoked."
            raise AuthenticationFailed(msg)
        if not token.user.is_active:
            msg = "User account is disabled."
            raise AuthenticationFailed(msg)
        self._touch_last_used(token)
        return token.user, token

    def authenticate_header(self, request):
        return "Bearer"

    @staticmethod
    def _touch_last_used(token: AISTApiToken) -> None:
        now = timezone.now()
        if (
            token.last_used_at is None
            or (now - token.last_used_at).total_seconds() > _LAST_USED_THROTTLE_SECONDS
        ):
            AISTApiToken.objects.filter(pk=token.pk).update(last_used_at=now)


class ScopedTokenAuthenticationScheme(OpenApiAuthenticationExtension):

    """drf-spectacular schema for ScopedTokenAuthentication (Bearer token)."""

    target_class = "aist.authentication.ScopedTokenAuthentication"
    name = "AISTApiToken"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "description": "AIST personal access token: `Authorization: Bearer aistpat_...`",
        }
