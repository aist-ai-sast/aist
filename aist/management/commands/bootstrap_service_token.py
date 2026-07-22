"""
Management command: bootstrap_service_token

Creates (or updates) a dedicated service account used by internal services
(e.g. context-extractor-mcp) to call the AIST API.

The token value is taken from the ``AIST_SERVICE_TOKEN`` environment variable.
If the variable is not set, a new token is generated and printed to stdout so
the operator can persist it in ``.env``.

The command is idempotent — safe to run on every deploy via the initializer.
"""
from __future__ import annotations

import os
import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from rest_framework.authtoken.models import Token

User = get_user_model()

_SERVICE_USERNAME = "aist-service"
_ENV_VAR = "AIST_SERVICE_TOKEN"


def _upsert_token(user, desired_key: str) -> tuple[Token, str]:
    try:
        token = Token.objects.get(user=user)
    except Token.DoesNotExist:
        return Token.objects.create(user=user, key=desired_key), "created"
    if token.key != desired_key:
        token.delete()
        return Token.objects.create(user=user, key=desired_key), "updated"
    return token, "unchanged"


class Command(BaseCommand):
    help = "Ensure the AIST internal service account and API token exist."

    def handle(self, *args, **options) -> None:
        desired_key = os.environ.get(_ENV_VAR, "").strip()

        if not desired_key:
            desired_key = secrets.token_hex(20)  # 40 hex chars — DRF Token max_length
            self.stdout.write(
                self.style.WARNING(
                    f"\n{_ENV_VAR} is not set. Generated a new token.\n"
                    f"Add the following line to your .env file:\n\n"
                    f"  {_ENV_VAR}={desired_key}\n",
                ),
            )

        with transaction.atomic():
            user, user_created = User.objects.get_or_create(
                username=_SERVICE_USERNAME,
                defaults={
                    "first_name": "AIST",
                    "last_name": "Service",
                    "email": "aist-service@internal.local",
                    "is_active": True,
                    "is_staff": True,
                    "is_superuser": True,
                },
            )
            if not user_created and not user.is_superuser:
                user.is_superuser = True
                user.is_staff = True
                user.save(update_fields=["is_superuser", "is_staff"])

            _token, action = _upsert_token(user, desired_key)

        verb = "Created" if user_created else "Found"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} service account '{_SERVICE_USERNAME}', token {action}.",
            ),
        )
