r"""
Management command: migrate_claude_env_to_integration

One-off helper for stands that still carry the legacy
``CLAUDE_CODE_OAUTH_TOKEN`` env var on celery / uwsgi containers. After
the Claude-as-OrgIntegration refactor (docs/plans/2026-05-12-claude-as-org-integration.md),
that token must live in an ``OrgIntegration(type=CLAUDE_CODE)`` row
scoped to a specific organisation; the env-based path is removed in
Task 9.

Usage::

    docker compose exec uwsgi python3 manage.py \\
        migrate_claude_env_to_integration --org <org-id>

Idempotent — re-running does not create duplicates. Prints a
reminder to remove the env var after successful migration so it
cannot quietly re-introduce a shared global Claude account.
"""
from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

from aist.models import Organization, OrgIntegration, OrgIntegrationType

_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"


class Command(BaseCommand):
    help = (
        "Migrate the legacy CLAUDE_CODE_OAUTH_TOKEN env var into an "
        "OrgIntegration row for the given organisation. Idempotent."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--org",
            type=int,
            required=True,
            help="ID of the AIST Organization that should own the Claude integration.",
        )
        parser.add_argument(
            "--name",
            type=str,
            default="default",
            help="Name for the new OrgIntegration row (default: 'default').",
        )

    def handle(self, *args, **options) -> None:
        token = (os.environ.get(_ENV_VAR) or "").strip()
        if not token:
            msg = (
                f"{_ENV_VAR} is not set in this process's environment. "
                "Set it before running this command, e.g.:\n"
                f"  {_ENV_VAR}=<token> python3 manage.py "
                "migrate_claude_env_to_integration --org <id>"
            )
            raise CommandError(msg)

        org_id: int = options["org"]
        try:
            org = Organization.objects.get(pk=org_id)
        except Organization.DoesNotExist as exc:
            msg = f"Organization with id={org_id} does not exist."
            raise CommandError(msg) from exc

        name: str = options["name"]
        existing = OrgIntegration.objects.filter(
            organization=org,
            integration_type=OrgIntegrationType.CLAUDE_CODE,
            name=name,
        ).first()
        if existing is not None:
            self.stdout.write(
                self.style.WARNING(
                    f"Claude integration '{name}' already exists for org "
                    f"'{org.name}' (id={org.pk}); skipping creation.",
                ),
            )
            return

        OrgIntegration.objects.create(
            organization=org,
            integration_type=OrgIntegrationType.CLAUDE_CODE,
            name=name,
            secret=token,
            is_active=True,
            config={"auth_mode": "oauth"},
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Created Claude integration '{name}' for org '{org.name}' "
                f"(id={org.pk}).",
            ),
        )
        self.stdout.write(
            self.style.WARNING(
                f"\nReminder: remove {_ENV_VAR} from your container env "
                "(docker-compose.yml / .env). After the refactor, the token "
                "is resolved per-pipeline from OrgIntegration and a stale "
                "env var risks reintroducing a global Claude account.",
            ),
        )
