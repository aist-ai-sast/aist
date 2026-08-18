from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from dojo.endpoint.utils import endpoint_get_or_create
from dojo.models import (
    Endpoint_Status,
    Engagement,
    Finding,
    Product,
    Product_Type_Member,
    Role,
    SLA_Configuration,
    Test,
    Test_Type,
)

from aist.models import (
    AISTAIFindingResponse,
    AISTAIResponse,
    AISTPipeline,
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    AISTStatus,
    DastExecutionOutcome,
    DastExecutionState,
    DastIntegrationState,
    DastIntegrationValidationState,
    DastProjectBinding,
    DastRunMetadata,
    DastTarget,
    LaunchSchedule,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    PipelineExecutionType,
    PipelineLaunchAuthorityKind,
    PipelineLaunchOrigin,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
    VersionType,
)
from aist.integrations.dast_config import DastLaunchRequirement
from aist.integrations.dast_report import (
    DastCoverage,
    DastTokenBucket,
    DastTokenUsage,
    ValidatedDastRunMetadata,
)


@dataclass(frozen=True, slots=True)
class DemoUserSpec:
    username: str
    first_name: str
    last_name: str
    email: str
    role_name: str
    organization_name: str


@dataclass(frozen=True, slots=True)
class DemoProjectSpec:
    slug: str
    organization_name: str
    product_name: str
    supported_languages: tuple[str, ...]
    compilable: bool
    project_age_days: int
    finding_distribution: tuple[int, ...]
    finding_day_offsets: tuple[int, ...]
    launch_config_name: str
    cron_expression: str
    schedule_last_run_days_ago: int
    queue_day_offsets: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DemoFindingTemplate:
    title: str
    severity: str
    cwe: int
    file_path: str
    vuln_id: str
    description: str
    mitigation: str


@dataclass(frozen=True, slots=True)
class DemoAIProfile:
    label: str
    verdict: str
    uncertainty_level: float | None
    uncertainty_spread: float | None
    impact_score: float | None
    exploitability_score: float | None
    epss_score: float | None
    exploit_code_maturity: str
    references: tuple[str, ...]
    reasoning: str
    fix: dict | None = None


ORG_NAMES = [
    "Acme Platform",
    "Nova Payments",
    "Helios Core",
]

# Test type name used by the DAST parser registration and demo records.
DAST_DEMO_SCAN_TYPE = "DAST Autonomous Scan"
MANUAL_DEMO_SCAN_TYPE = "Demo Manual Report Import"
DAST_DEMO_RUN_COUNT = 3
MANUAL_DEMO_IMPORT_COUNT = 3

# Reported run metadata for the demo DAST runs, so the pipeline card's coverage and spend panels
# have something to show. Shaped like a real report's dast_run_metadata (see
# docs/integrations/dast.md), one entry per historical run.
#
# The plan/analysed relationship deliberately differs across the three: run 1 overshot its plan by
# a lot, run 2 stayed inside it, run 3 overshot slightly — so the conditional "beyond plan" chip is
# visible on some rows and absent on others rather than always on.
DAST_DEMO_COVERAGE = (
    # (discovered, reachable, analysed, planned)
    (784, 176, 38, 10),
    (312, 96, 24, 30),
    (540, 130, 31, 25),
)
# (input, output, thinking, cache_creation, cache_read, calls) per run, in the proportions a real
# agent run produces — cache reads dominate, direct input is negligible.
DAST_DEMO_TOKEN_TOTALS = (
    (2234, 951808, 331554, 2578204, 90024238, 1117),
    (874, 372615, 129804, 1009436, 35240117, 437),
    (1508, 642391, 223760, 1740083, 60746902, 754),
)
# Phase and agent-type shares. Each split adds back up to the run total exactly, so the demo data
# is internally consistent and never trips the accounting-mismatch note.
DAST_DEMO_PHASES = (
    ("5", "regression", 13),
    ("6", "depth: floor, explore, discovery", 61),
    ("7", "verify", 18),
    ("8", "audit, report, durability", 8),
)
DAST_DEMO_AGENT_TYPES = (
    ("orchestrator", 1, 24),
    ("dast-discovery", 1, 10),
    ("dast-check-runner", 9, 52),
    ("dast-verify", 4, 14),
)
_DAST_DEMO_COVERAGE_SERVICES = (
    "auth",
    "cdb",
    "discovery",
    "licensing",
    "mediator",
    "oauth2-server",
    "log-collector",
    "service-authorizer",
    "speedtest",
    "stats",
)
_DAST_DEMO_COVERAGE_STANDS = ("prod", "stage", "test", "dev")


def _split_exactly(total: int, weights: tuple[int, ...]) -> list[int]:
    """Split an integer into weighted shares that always add back up to it."""
    denominator = sum(weights)
    shares = [total * weight // denominator for weight in weights[:-1]]
    return [*shares, total - sum(shares)]


def demo_coverage_names(count: int) -> tuple[str, ...]:
    """Plausible endpoint names for the demo coverage inventory, on the reserved example.com."""
    names = [
        f"{service}-{stand}.example.com"
        for stand in _DAST_DEMO_COVERAGE_STANDS
        for service in _DAST_DEMO_COVERAGE_SERVICES
    ]
    return tuple(names[:count])


def demo_ai_filter_snapshot() -> dict:
    return {
        "limit": 50,
        "severity": [{"comparison": "EQUALS", "value": "HIGH"}],
    }


DEMO_USERS = [
    DemoUserSpec("org_reader", "Org", "Reader", "org_reader@example.local", "Reader", "Nova Payments"),
    DemoUserSpec("org_writer", "Org", "Writer", "org_writer@example.local", "Writer", "Nova Payments"),
    DemoUserSpec("org_maintainer", "Org", "Maintainer", "org_maintainer@example.local", "Maintainer", "Nova Payments"),
    DemoUserSpec("org_owner", "Org", "Owner", "org_owner@example.local", "Owner", "Nova Payments"),
    DemoUserSpec("acme_reader", "Acme", "Reader", "acme_reader@example.local", "Reader", "Acme Platform"),
    DemoUserSpec("acme_maintainer", "Acme", "Maintainer", "acme_maintainer@example.local", "Maintainer", "Acme Platform"),
    DemoUserSpec("helios_maintainer", "Helios", "Maintainer", "helios_maintainer@example.local", "Maintainer", "Helios Core"),
]

DEMO_PROJECTS = [
    DemoProjectSpec(
        slug="payments-api",
        organization_name="Nova Payments",
        product_name="Demo AIST Payments API",
        supported_languages=("python", "go"),
        compilable=True,
        project_age_days=45,
        finding_distribution=(7, 3, 6, 5, 4),
        finding_day_offsets=(2, 5, 9, 15, 24),
        launch_config_name="Nightly security baseline",
        cron_expression="15 1 * * *",
        schedule_last_run_days_ago=1,
        queue_day_offsets=(14, 11, 8, 5, 3, 1),
    ),
    DemoProjectSpec(
        slug="checkout-web",
        organization_name="Acme Platform",
        product_name="Demo AIST Checkout Web",
        supported_languages=("typescript",),
        compilable=False,
        project_age_days=38,
        finding_distribution=(6, 4, 5, 5, 4),
        finding_day_offsets=(1, 4, 8, 12, 21),
        launch_config_name="Checkout daily scan",
        cron_expression="30 2 * * *",
        schedule_last_run_days_ago=2,
        queue_day_offsets=(13, 10, 7, 4, 2, 1),
    ),
    DemoProjectSpec(
        slug="identity-service",
        organization_name="Acme Platform",
        product_name="Demo AIST Identity Service",
        supported_languages=("java", "kotlin"),
        compilable=True,
        project_age_days=60,
        finding_distribution=(8, 5, 4, 6, 3),
        finding_day_offsets=(3, 7, 11, 18, 27),
        launch_config_name="Identity weekly sweep",
        cron_expression="0 4 * * 1",
        schedule_last_run_days_ago=4,
        queue_day_offsets=(21, 17, 13, 9, 6, 2),
    ),
    DemoProjectSpec(
        slug="ledger-worker",
        organization_name="Nova Payments",
        product_name="Demo AIST Ledger Worker",
        supported_languages=("rust",),
        compilable=True,
        project_age_days=52,
        finding_distribution=(7, 4, 6, 4, 5),
        finding_day_offsets=(2, 6, 10, 16, 25),
        launch_config_name="Ledger periodic scan",
        cron_expression="45 3 * * 2,5",
        schedule_last_run_days_ago=3,
        queue_day_offsets=(20, 16, 12, 8, 5, 1),
    ),
    DemoProjectSpec(
        slug="analytics-core",
        organization_name="Helios Core",
        product_name="Demo AIST Analytics Core",
        supported_languages=("python", "sql"),
        compilable=True,
        project_age_days=70,
        finding_distribution=(5, 7, 4, 6, 3),
        finding_day_offsets=(4, 9, 14, 20, 30),
        launch_config_name="Analytics hardening run",
        cron_expression="10 5 * * 1,3",
        schedule_last_run_days_ago=5,
        queue_day_offsets=(24, 19, 14, 10, 6, 2),
    ),
]

DEMO_FINDING_TEMPLATES = [
    DemoFindingTemplate(
        title="Hardcoded cloud access key in deployment script",
        severity="High",
        cwe=798,
        file_path="deploy/scripts/release.sh",
        vuln_id="DEMO-SEC-001",
        description="A static token is committed directly in repository scripts.",
        mitigation="Move credentials to secret manager and rotate impacted tokens.",
    ),
    DemoFindingTemplate(
        title="SQL query built from unsanitized input",
        severity="Critical",
        cwe=89,
        file_path="src/api/reporting/query_builder.py",
        vuln_id="DEMO-SEC-002",
        description="String interpolation is used to construct SQL clauses.",
        mitigation="Use parameterized queries and add validation for filter values.",
    ),
    DemoFindingTemplate(
        title="Insecure TLS verification disabled for outbound call",
        severity="Medium",
        cwe=295,
        file_path="src/integrations/payment_gateway/client.py",
        vuln_id="DEMO-SEC-003",
        description="Certificate validation is switched off for convenience path.",
        mitigation="Enable strict certificate validation with proper trust store.",
    ),
    DemoFindingTemplate(
        title="Path traversal risk in archive extraction endpoint",
        severity="High",
        cwe=22,
        file_path="src/services/import/archive_handler.go",
        vuln_id="DEMO-SEC-004",
        description="Archive members are written without canonical path checks.",
        mitigation="Normalize paths and reject entries escaping target directory.",
    ),
    DemoFindingTemplate(
        title="Verbose error output leaks internal service topology",
        severity="Low",
        cwe=200,
        file_path="src/http/middleware/error_responses.ts",
        vuln_id="DEMO-SEC-005",
        description="Unhandled exceptions expose internal hosts and stack traces.",
        mitigation="Return generic error messages and log details server-side only.",
    ),
]

DEMO_AI_PROFILES = (
    DemoAIProfile(
        label="high-confidence-tp",
        verdict=AISTAIFindingResponse.Verdict.TRUE_POSITIVE,
        uncertainty_level=0.08,
        uncertainty_spread=0.05,
        impact_score=9.2,
        exploitability_score=8.8,
        epss_score=0.93,
        exploit_code_maturity="high",
        references=(
            "https://owasp.org/Top10/A03_2021-Injection/",
            "https://cwe.mitre.org/data/definitions/89.html",
        ),
        reasoning="Strong evidence indicates the issue is exploitable and directly reachable by untrusted input.",
        fix={
            "fixType": "code_change",
            "fixSummary": (
                "Replace the hardcoded secret with a reference to an environment variable and add "
                "startup validation so the service fails fast when credentials are absent."
            ),
            "diff": (
                "--- a/deploy/scripts/release.sh\n"
                "+++ b/deploy/scripts/release.sh\n"
                "@@ -12,7 +12,11 @@\n"
                '-CLOUD_ACCESS_KEY="AKIA...HARDCODED"\n'
                '-CLOUD_SECRET_KEY="wJalrXUt...HARDCODED"\n'
                '+if [ -z "$CLOUD_ACCESS_KEY" ] || [ -z "$CLOUD_SECRET_KEY" ]; then\n'
                '+  echo "ERROR: CLOUD_ACCESS_KEY and CLOUD_SECRET_KEY must be set" >&2\n'
                "+  exit 1\n"
                "+fi\n"
                ' aws configure set aws_access_key_id     "$CLOUD_ACCESS_KEY"\n'
                ' aws configure set aws_secret_access_key "$CLOUD_SECRET_KEY"'
            ),
            "diffAvailable": True,
            "codeAfter": (
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n\n"
                "# Credentials injected by CI/CD pipeline or secret manager\n"
                'if [ -z "${CLOUD_ACCESS_KEY:-}" ] || [ -z "${CLOUD_SECRET_KEY:-}" ]; then\n'
                '  echo "ERROR: CLOUD_ACCESS_KEY and CLOUD_SECRET_KEY environment variables are required." >&2\n'
                "  exit 1\n"
                "fi\n\n"
                'aws configure set aws_access_key_id     "$CLOUD_ACCESS_KEY"\n'
                'aws configure set aws_secret_access_key "$CLOUD_SECRET_KEY"\n'
                'aws configure set default.region        "${AWS_REGION:-us-east-1}"'
            ),
            "stepByStep": [
                "Step 1: Open deploy/scripts/release.sh and locate the hardcoded CLOUD_ACCESS_KEY and CLOUD_SECRET_KEY assignments.",
                "Step 2: Delete both hardcoded lines and replace with references to the same-named environment variables.",
                "Step 3: Add a guard block at the top of the script that exits with a non-zero code if either variable is unset.",
                "Step 4: Store the actual secrets in your CI/CD secret store (GitHub Actions Secrets, GitLab CI Variables, or Vault).",
                "Step 5: Run `git log -p --all -S 'AKIA' -- '*.sh'` to confirm no hardcoded keys remain in commit history.",
                "Step 6: Rotate the exposed access key immediately via the cloud provider console and revoke the old one.",
            ],
            "testingHint": (
                "1. Unset the environment variables and run the script — it must exit with code 1 and print the error message.\n"
                "2. Export valid test credentials and confirm the script proceeds normally.\n"
                "3. Run `git secrets --scan` or `trufflehog git file://.` to verify no secrets remain in the repo."
            ),
            "secretsManagement": (
                "Inject CLOUD_ACCESS_KEY and CLOUD_SECRET_KEY via your CI/CD platform's native secret store. "
                "For AWS workloads prefer IAM roles over long-lived keys. "
                "If keys are required, use AWS Secrets Manager or HashiCorp Vault with short TTLs and automatic rotation."
            ),
            "suppressionAnnotation": None,
        },
    ),
    DemoAIProfile(
        label="medium-confidence-tp",
        verdict=AISTAIFindingResponse.Verdict.TRUE_POSITIVE,
        uncertainty_level=0.44,
        uncertainty_spread=0.18,
        impact_score=6.1,
        exploitability_score=5.7,
        epss_score=0.42,
        exploit_code_maturity="functional",
        references=(
            "https://owasp.org/www-project-cheat-sheets/",
        ),
        reasoning="Likely true positive with partial exploit path confidence; remediation is still recommended.",
        fix={
            "fixType": "code_change",
            "fixSummary": (
                "Replace string-interpolated SQL with parameterized queries to eliminate injection risk."
            ),
            "diff": (
                "--- a/src/api/reporting/query_builder.py\n"
                "+++ b/src/api/reporting/query_builder.py\n"
                "@@ -18,8 +18,9 @@\n"
                " def build_report_query(cursor, filters: dict):\n"
                "-    status_clause = f\"AND status = '{filters['status']}'\"\n"
                "-    sql = f\"SELECT * FROM reports WHERE tenant_id = {filters['tenant_id']} {status_clause}\"\n"
                "-    cursor.execute(sql)\n"
                '+    sql = "SELECT * FROM reports WHERE tenant_id = %s AND status = %s"\n'
                '+    params = (filters["tenant_id"], filters["status"])\n'
                "+    cursor.execute(sql, params)"
            ),
            "diffAvailable": True,
            "codeAfter": (
                'ALLOWED_STATUSES = frozenset({"open", "closed", "pending"})\n\n'
                "def build_report_query(cursor, filters: dict):\n"
                '    tenant_id = filters["tenant_id"]\n'
                '    status = filters["status"]\n\n'
                "    if status not in ALLOWED_STATUSES:\n"
                '        raise ValueError(f"Invalid status value: {status!r}")\n\n'
                '    sql = "SELECT * FROM reports WHERE tenant_id = %s AND status = %s"\n'
                "    cursor.execute(sql, (tenant_id, status))"
            ),
            "stepByStep": [
                "Step 1: Open src/api/reporting/query_builder.py and locate all f-string or %-formatted SQL statements.",
                "Step 2: Extract each dynamic value into a separate variable for clarity.",
                "Step 3: Replace the interpolated SQL with a parameterized template using %s placeholders.",
                "Step 4: Pass the values as the second argument tuple to cursor.execute().",
                "Step 5: Add an allowlist check for enumerable fields like 'status' to add defense-in-depth.",
                "Step 6: Run the existing test suite and add a test that passes a SQL-injection payload (e.g., \"'; DROP TABLE reports; --\") and asserts it is rejected or returned as literal data.",
            ],
            "testingHint": (
                "Send status=\"'; DROP TABLE reports; --\" through the API endpoint and verify:\n"
                "  - No SQL error is raised\n"
                "  - The value is treated as a literal string filter, not executed\n"
                "Use sqlmap or a manual curl request to confirm the endpoint is no longer injectable."
            ),
            "secretsManagement": None,
            "suppressionAnnotation": None,
        },
    ),
    DemoAIProfile(
        label="low-confidence-uncertain",
        verdict=AISTAIFindingResponse.Verdict.UNCERTAIN,
        uncertainty_level=0.86,
        uncertainty_spread=0.64,
        impact_score=3.2,
        exploitability_score=2.6,
        epss_score=0.08,
        exploit_code_maturity="proof_of_concept",
        references=(),
        reasoning="Signal is weak and may require manual triage with runtime context before a final verdict.",
        fix=None,
    ),
    DemoAIProfile(
        label="high-confidence-fp",
        verdict=AISTAIFindingResponse.Verdict.FALSE_POSITIVE,
        uncertainty_level=0.11,
        uncertainty_spread=0.07,
        impact_score=1.3,
        exploitability_score=1.1,
        epss_score=0.01,
        exploit_code_maturity="unproven",
        references=(
            "https://owasp.org/www-community/vulnerabilities/False_Positive",
        ),
        reasoning="Pattern appears non-exploitable in this code path; scanner result is likely a false positive.",
        fix=None,
    ),
    DemoAIProfile(
        label="medium-confidence-uncertain",
        verdict=AISTAIFindingResponse.Verdict.UNCERTAIN,
        uncertainty_level=0.58,
        uncertainty_spread=0.24,
        impact_score=4.8,
        exploitability_score=3.9,
        epss_score=0.17,
        exploit_code_maturity="functional",
        references=(
            "https://cwe.mitre.org/",
            "https://nvd.nist.gov/",
        ),
        reasoning="Conflicting static signals detected; additional validation is needed before closure or acceptance.",
        fix={
            "fixType": "architectural",
            "fixSummary": (
                "Conduct a targeted threat-model review of this component before committing to a code-level fix; "
                "the finding may require a design change rather than a patch."
            ),
            "diff": None,
            "diffAvailable": False,
            "codeAfter": None,
            "stepByStep": [
                "Step 1: Schedule a 60-minute threat-model session with the feature team and a security champion.",
                "Step 2: Map all data flows that pass through the flagged component using a data-flow diagram.",
                "Step 3: Identify trust boundaries and verify whether untrusted input can reach the sensitive operation.",
                "Step 4: If a real risk is confirmed, raise a tracked remediation task with severity and target sprint.",
                "Step 5: If the risk is accepted, document the rationale in the project's security decision log and link it to this finding.",
            ],
            "testingHint": (
                "After the threat-model session, run a focused penetration test or manual code review "
                "on the identified data flows to validate whether exploitation is feasible in production."
            ),
            "secretsManagement": None,
            "suppressionAnnotation": None,
        },
    ),
    DemoAIProfile(
        label="sparse-data-uncertain",
        verdict=AISTAIFindingResponse.Verdict.UNCERTAIN,
        uncertainty_level=None,
        uncertainty_spread=None,
        impact_score=None,
        exploitability_score=None,
        epss_score=None,
        exploit_code_maturity="",
        references=(),
        reasoning="Insufficient context from source artifact to produce quantitative confidence metrics.",
        fix=None,
    ),
)


class Command(BaseCommand):
    help = (
        "Bootstrap demo admin username, organizations and users. "
        "Creates organizations/users, binds organization roles via ProductType membership, "
        "and prepares demo AIST projects with findings, launch configs, schedules and queue history."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default="ChangeMe123!",
            help="Password set for created demo users (and optionally for superuser).",
        )
        parser.add_argument(
            "--skip-admin",
            action="store_true",
            default=False,
            help="Do not modify superuser username/password.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        password = options["password"]
        skip_admin = options["skip_admin"]

        user_model = get_user_model()

        if not skip_admin:
            self._ensure_admin_username(user_model, password=password)

        users_by_username = self._ensure_demo_users(user_model, password=password)
        organizations = self._ensure_organizations()

        self._bind_roles_via_product_type(organizations=organizations, users_by_username=users_by_username)
        self._ensure_demo_projects(organizations=organizations, users_by_username=users_by_username)

        self.stdout.write(f"Demo users password: {password}")
        self.stdout.write(f"Organizations: {', '.join(org.name for org in organizations)}")
        self.stdout.write(f"Demo projects: {', '.join(spec.product_name for spec in DEMO_PROJECTS)}")

    def _ensure_admin_username(self, user_model, *, password: str) -> None:
        admin = user_model.objects.filter(is_superuser=True).order_by("id").first()
        if admin is None:
            self.stdout.write("Superuser not found, skip admin rename.")
            return

        if admin.username != "admin":
            existing_admin = user_model.objects.exclude(pk=admin.pk).filter(username="admin").first()
            if existing_admin is not None:
                suffix = 1
                alias = "admin_legacy"
                while user_model.objects.filter(username=alias).exists():
                    suffix += 1
                    alias = f"admin_legacy_{suffix}"
                existing_admin.username = alias
                existing_admin.save(update_fields=["username"])
            admin.username = "admin"
            admin.save(update_fields=["username"])

        if not admin.check_password(password):
            admin.set_password(password)
            admin.save(update_fields=["password"])

    def _ensure_demo_users(self, user_model, *, password: str) -> dict[str, object]:
        users_by_username: dict[str, object] = {}
        for spec in DEMO_USERS:
            user, _ = user_model.objects.get_or_create(
                username=spec.username,
                defaults={
                    "first_name": spec.first_name,
                    "last_name": spec.last_name,
                    "email": spec.email,
                    "is_active": True,
                },
            )
            updates: list[str] = []
            if user.first_name != spec.first_name:
                user.first_name = spec.first_name
                updates.append("first_name")
            if user.last_name != spec.last_name:
                user.last_name = spec.last_name
                updates.append("last_name")
            if user.email != spec.email:
                user.email = spec.email
                updates.append("email")
            if updates:
                user.save(update_fields=updates)

            if not user.check_password(password):
                user.set_password(password)
                user.save(update_fields=["password"])
            users_by_username[spec.username] = user
        return users_by_username

    def _ensure_organizations(self) -> list[Organization]:
        organizations: list[Organization] = []
        for idx, org_name in enumerate(ORG_NAMES, start=1):
            org, _ = Organization.objects.get_or_create(
                name=org_name,
                defaults={"description": f"Demo organization {idx}"},
            )
            org.ensure_product_type()
            organizations.append(org)
        return organizations

    def _bind_roles_via_product_type(self, *, organizations: list[Organization], users_by_username: dict[str, object]) -> None:
        role_names = {spec.role_name for spec in DEMO_USERS}
        roles = {role.name: role for role in Role.objects.filter(name__in=role_names)}
        missing_roles = sorted(role_names - set(roles))
        if missing_roles:
            msg = f"Missing roles in DB: {', '.join(missing_roles)}"
            raise CommandError(msg)

        organizations_by_name = {org.name: org for org in organizations}
        all_demo_users = list(users_by_username.values())
        all_org_product_type_ids = [org.ensure_product_type().id for org in organizations]

        # Remove any memberships that point demo users to orgs outside their assigned org.
        Product_Type_Member.objects.filter(
            user__in=all_demo_users,
            product_type_id__in=all_org_product_type_ids,
        ).delete()

        for spec in DEMO_USERS:
            org = organizations_by_name.get(spec.organization_name)
            if org is None:
                msg = f"Organization '{spec.organization_name}' not found for user '{spec.username}'"
                raise CommandError(msg)
            product_type = org.ensure_product_type()
            Product_Type_Member.objects.get_or_create(
                product_type=product_type,
                user=users_by_username[spec.username],
                defaults={"role": roles[spec.role_name]},
            )

    def _ensure_demo_dast_integrations(
        self,
        *,
        organizations: list[Organization],
        now,
    ) -> dict[str, list[DastTarget]]:
        targets_by_organization: dict[str, list[DastTarget]] = {}
        project_slugs_by_organization = {
            organization.name: [
                spec.slug
                for spec in DEMO_PROJECTS
                if spec.organization_name == organization.name
            ]
            for organization in organizations
        }
        # Matches the real DAST target contract: every currently wired provider target
        # (dast/targets/*/target.yaml in the DAST repo) publishes this exact single-field
        # schema and the same "light" default -- there is no real per-target variation to
        # demonstrate here, so the demo schema mirrors reality rather than inventing one.
        parameter_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "depth": {
                    "type": "string",
                    "title": "Scan depth",
                    "enum": ["light", "deep"],
                },
            },
            "required": ["depth"],
        }
        for organization_index, organization in enumerate(organizations, start=1):
            organization_key = organization.name.lower().replace(" ", "-")
            integration, _ = OrgIntegration.objects.get_or_create(
                organization=organization,
                integration_type=OrgIntegrationType.DAST,
                name="Demo DAST gateway",
                defaults={
                    "config": {
                        "gateway_url": f"https://dast-{organization_key}.example",
                        "ca_bundle": "",
                        "contract_major": 2,
                        "integrator_public_id": f"demo_{organization_key}",
                        "server_fingerprint": f"sha256:demo-{organization_key}-fingerprint",
                    },
                    "is_active": True,
                },
            )
            integration_updates: list[str] = []
            if not integration.is_active:
                integration.is_active = True
                integration_updates.append("is_active")
            if integration_updates:
                integration.save(update_fields=[*integration_updates, "updated"])

            DastIntegrationState.objects.update_or_create(
                integration=integration,
                defaults={
                    "validation_state": DastIntegrationValidationState.READY,
                    "validated_at": now,
                    "contract_version": "2.0",
                    "capabilities_etag": f"demo-catalog-{organization_index}",
                    "capabilities_synced_at": now,
                },
            )

            repository_keys = project_slugs_by_organization[organization.name]
            self._retire_superseded_demo_target_ids(integration)
            targets: list[DastTarget] = []
            for target_index, (provider_id, display_name, defaults, launch_requirements, target_repository_keys) in enumerate(
                (
                    (
                        "demo-browser", "Customer web application", {"depth": "light"},
                        [DastLaunchRequirement.REPOSITORY_TRIGGER.value], repository_keys,
                    ),
                    (
                        "demo-api", "Public API surface", {"depth": "light"},
                        [DastLaunchRequirement.REPOSITORY_TRIGGER.value], repository_keys,
                    ),
                    # Sourceless scenario: a perimeter/blackbox scan of a fixed public surface, not
                    # tied to any commit. Unlike the two invented surfaces above, this id is not
                    # ours to make up — the provider target is `perimeter`
                    # (`dast/targets/perimeter/target.yaml`, `name: perimeter`) and every perimeter
                    # report says so in its `dast_run_metadata.target`. A "demo-" prefix here would
                    # mean no demo binding could ever accept a real perimeter report.
                    ("perimeter", "Public perimeter surface", {"depth": "light"}, [], []),
                ),
                start=1,
            ):
                target, _ = DastTarget.objects.update_or_create(
                    integration=integration,
                    provider_id=provider_id,
                    defaults={
                        "display_name": display_name,
                        "contract_revision": "2.0",
                        "capability_revision": f"sha256:demo-{organization_index}-{provider_id}-capability-v1",
                        "schema_digest": f"sha256:demo-{organization_index}-{provider_id}-schema-v1",
                        "parameter_schema": parameter_schema,
                        "provider_defaults": defaults,
                        "repository_keys": target_repository_keys,
                        "launch_requirements": launch_requirements,
                        "autonomous_ready": True,
                        "is_available": True,
                        "last_seen_at": now - timedelta(minutes=target_index),
                    },
                )
                target.full_clean()
                targets.append(target)
            targets_by_organization[organization.name] = targets
        return targets_by_organization

    # Demo target ids that an earlier seed made up before the real provider id was known. Renamed
    # in place rather than re-seeded, so the bindings, launch configs and pipelines already pointing
    # at the row follow it instead of being orphaned behind a target nothing publishes any more.
    SUPERSEDED_DEMO_TARGET_IDS = {"demo-perimeter": "perimeter"}

    def _retire_superseded_demo_target_ids(self, integration: OrgIntegration) -> None:
        for stale_id, provider_id in self.SUPERSEDED_DEMO_TARGET_IDS.items():
            if DastTarget.objects.filter(integration=integration, provider_id=provider_id).exists():
                continue
            DastTarget.objects.filter(integration=integration, provider_id=stale_id).update(provider_id=provider_id)

    def _ensure_demo_projects(self, *, organizations: list[Organization], users_by_username: dict[str, object]) -> None:
        organizations_by_name = {org.name: org for org in organizations}
        first_maintainer_username = next(s.username for s in DEMO_USERS if s.role_name == "Maintainer")
        default_reporter = users_by_username[first_maintainer_username]
        now = timezone.now()
        today = timezone.localdate()
        sla_config, _ = SLA_Configuration.objects.get_or_create(name="Demo SLA")
        test_type, _ = Test_Type.objects.get_or_create(name="Semgrep JSON Report")
        dast_targets_by_organization = self._ensure_demo_dast_integrations(
            organizations=organizations,
            now=now,
        )

        for spec in DEMO_PROJECTS:
            organization = organizations_by_name[spec.organization_name]
            product_type = organization.ensure_product_type()

            product, _ = Product.objects.get_or_create(
                name=spec.product_name,
                defaults={
                    "description": f"Demo product for {spec.slug}",
                    "prod_type": product_type,
                    "sla_configuration": sla_config,
                },
            )

            product_updates: list[str] = []
            if product.prod_type_id != product_type.id:
                product.prod_type = product_type
                product_updates.append("prod_type")
            if product.sla_configuration_id != sla_config.id:
                product.sla_configuration = sla_config
                product_updates.append("sla_configuration")
            desired_description = f"Demo product for {spec.slug}"
            if product.description != desired_description:
                product.description = desired_description
                product_updates.append("description")
            if product_updates:
                product.save(update_fields=product_updates)

            project, _ = AISTProject.objects.get_or_create(
                product=product,
                defaults={
                    "supported_languages": list(spec.supported_languages),
                    "compilable": spec.compilable,
                    "profile": {"team": "application-security", "environment": "demo"},
                },
            )
            project_updates: list[str] = []
            supported_languages = list(spec.supported_languages)
            if project.supported_languages != supported_languages:
                project.supported_languages = supported_languages
                project_updates.append("supported_languages")
            if project.compilable != spec.compilable:
                project.compilable = spec.compilable
                project_updates.append("compilable")
            if project_updates:
                project.save(update_fields=project_updates)

            project_created_at = now - timedelta(days=spec.project_age_days)
            AISTProject.objects.filter(pk=project.pk).update(created=project_created_at)

            main_version, _ = AISTProjectVersion.objects.get_or_create(
                project=project,
                version="main",
                version_type=VersionType.GIT_BRANCH,
                defaults={"description": "Default demo branch"},
            )
            release_version, _ = AISTProjectVersion.objects.get_or_create(
                project=project,
                version="release-v1",
                version_type=VersionType.GIT_BRANCH,
                defaults={"description": "Stable demo release"},
            )

            dast_bindings, dast_launch_configs = self._ensure_project_dast_bindings_and_configs(
                spec=spec,
                project=project,
                trigger_version=main_version,
                targets=dast_targets_by_organization[spec.organization_name],
            )

            launch_config, _ = AISTProjectLaunchConfig.objects.get_or_create(
                project=project,
                name=spec.launch_config_name,
                defaults={
                    "execution_type": PipelineExecutionType.SAST,
                    "description": f"Demo launch config for {spec.slug}",
                    "params": {
                        "analyzers": ["semgrep", "snyk"],
                        "time_class_level": "normal",
                        "log_level": "INFO",
                        "ai_mode": "AUTO_DEFAULT",
                        "ai_filter_snapshot": demo_ai_filter_snapshot(),
                    },
                    "is_default": True,
                },
            )

            launch_config_updates: list[str] = []
            if launch_config.execution_type != PipelineExecutionType.SAST:
                launch_config.execution_type = PipelineExecutionType.SAST
                launch_config_updates.append("execution_type")
            if launch_config.dast_binding_id is not None:
                launch_config.dast_binding = None
                launch_config_updates.append("dast_binding")
            if launch_config.trigger_project_version_id is not None:
                launch_config.trigger_project_version = None
                launch_config_updates.append("trigger_project_version")
            desired_params = {
                "analyzers": ["semgrep", "snyk"],
                "time_class_level": "normal",
                "log_level": "INFO",
                "ai_mode": "AUTO_DEFAULT",
                "ai_filter_snapshot": demo_ai_filter_snapshot(),
            }
            if launch_config.params != desired_params:
                launch_config.params = desired_params
                launch_config_updates.append("params")
            desired_cfg_description = f"Demo launch config for {spec.slug}"
            if launch_config.description != desired_cfg_description:
                launch_config.description = desired_cfg_description
                launch_config_updates.append("description")
            if not launch_config.is_default:
                launch_config.is_default = True
                launch_config_updates.append("is_default")
            if launch_config_updates:
                launch_config.save(update_fields=launch_config_updates)

            schedule, _ = LaunchSchedule.objects.get_or_create(
                launch_config=launch_config,
                defaults={
                    "cron_expression": spec.cron_expression,
                    "enabled": True,
                    "max_concurrent_runs": 1,
                },
            )
            schedule.last_run_at = now - timedelta(days=spec.schedule_last_run_days_ago)
            schedule_updates = ["last_run_at"]
            if schedule.cron_expression != spec.cron_expression:
                schedule.cron_expression = spec.cron_expression
                schedule_updates.append("cron_expression")
            if not schedule.enabled:
                schedule.enabled = True
                schedule_updates.append("enabled")
            if schedule.max_concurrent_runs != 1:
                schedule.max_concurrent_runs = 1
                schedule_updates.append("max_concurrent_runs")
            desired_next_run_at = schedule.get_next_scheduled_time(now=now)
            if schedule.next_run_at != desired_next_run_at:
                schedule.next_run_at = desired_next_run_at
                schedule_updates.append("next_run_at")
            schedule.save(update_fields=schedule_updates)

            engagement, _ = Engagement.objects.get_or_create(
                product=product,
                name=f"{spec.slug} security engagement",
                defaults={
                    "target_start": project_created_at,
                    "target_end": today + timedelta(days=365),
                },
            )
            if engagement.target_end < today:
                engagement.target_end = today + timedelta(days=365)
                engagement.save(update_fields=["target_end"])

            dojo_test, _ = Test.objects.get_or_create(
                engagement=engagement,
                test_type=test_type,
                title=f"{spec.slug} baseline scan",
                defaults={
                    "target_start": project_created_at,
                    "target_end": now,
                },
            )

            finding_ids = self._ensure_project_findings(
                spec=spec,
                dojo_test=dojo_test,
                reporter=default_reporter,
                base_date=today,
            )
            main_version.findings.add(*finding_ids)
            release_ids = [finding_id for idx, finding_id in enumerate(finding_ids, start=1) if idx % 2 == 0]
            if release_ids:
                release_version.findings.add(*release_ids)

            self._ensure_historical_queue(
                spec=spec,
                project=project,
                main_version=main_version,
                release_version=release_version,
                launch_config=launch_config,
                schedule=schedule,
                now=now,
            )
            self._ensure_historical_dast_runs(
                spec=spec,
                project=project,
                main_version=main_version,
                release_version=release_version,
                bindings=dast_bindings,
                launch_configs=dast_launch_configs,
                engagement=engagement,
                reporter=default_reporter,
                now=now,
                base_date=today,
            )
            self._ensure_historical_manual_imports(
                spec=spec,
                project=project,
                main_version=main_version,
                release_version=release_version,
                engagement=engagement,
                reporter=default_reporter,
                now=now,
                base_date=today,
            )
            self._ensure_demo_ai_responses(project=project)

    def _ensure_project_dast_bindings_and_configs(
        self,
        *,
        spec: DemoProjectSpec,
        project: AISTProject,
        trigger_version: AISTProjectVersion,
        targets: list[DastTarget],
    ) -> tuple[list[DastProjectBinding], list[AISTProjectLaunchConfig]]:
        bindings: list[DastProjectBinding] = []
        launch_configs: list[AISTProjectLaunchConfig] = []
        for target_index, target in enumerate(targets, start=1):
            parameters = dict(target.provider_defaults)
            requires_source = target.get_snapshot().launch_requirements.requires_repository()
            binding_trigger_version = trigger_version if requires_source else None
            binding, _ = DastProjectBinding.objects.update_or_create(
                project=project,
                target=target,
                defaults={
                    "source_repo_key": spec.slug if requires_source else "",
                    "enabled": True,
                    "parameter_snapshot": parameters,
                },
            )
            binding.full_clean()
            bindings.append(binding)

            config_name = f"DAST · {target.display_name}"
            launch_config, _ = AISTProjectLaunchConfig.objects.get_or_create(
                project=project,
                name=config_name,
                defaults={
                    "execution_type": PipelineExecutionType.DAST,
                    "dast_binding": binding,
                    "trigger_project_version": binding_trigger_version,
                    "description": f"Demo DAST preset for {spec.slug} on {target.display_name}",
                    "params": parameters,
                    "is_default": False,
                },
            )
            launch_config.execution_type = PipelineExecutionType.DAST
            launch_config.dast_binding = binding
            launch_config.trigger_project_version = binding_trigger_version
            launch_config.description = f"Demo DAST preset for {spec.slug} on {target.display_name}"
            launch_config.params = parameters
            launch_config.is_default = False
            launch_config.full_clean()
            launch_config.save()
            launch_configs.append(launch_config)

            LaunchSchedule.objects.update_or_create(
                launch_config=launch_config,
                defaults={
                    "cron_expression": f"{10 + target_index * 5} 6 * * {target_index}",
                    "enabled": False,
                    "max_concurrent_runs": 1,
                    "last_run_at": None,
                    "next_run_at": None,
                },
            )
        return bindings, launch_configs

    def _ensure_project_findings(self, *, spec: DemoProjectSpec, dojo_test: Test, reporter, base_date):
        finding_ids: list[int] = []
        sequence = 1
        for day_offset, findings_count in zip(spec.finding_day_offsets, spec.finding_distribution, strict=True):
            finding_date = base_date - timedelta(days=day_offset)
            for _ in range(findings_count):
                template = DEMO_FINDING_TEMPLATES[(sequence - 1) % len(DEMO_FINDING_TEMPLATES)]
                title = f"{template.title} [{spec.slug.upper()}-{sequence:03d}]"
                desired_vuln_id = f"{template.vuln_id}-{sequence:03d}"
                finding, _ = Finding.objects.get_or_create(
                    test=dojo_test,
                    vuln_id_from_tool=desired_vuln_id,
                    defaults={
                        "title": title,
                        "severity": template.severity,
                        "cwe": template.cwe,
                        "date": finding_date,
                        "reporter": reporter,
                        "file_path": template.file_path,
                        "description": template.description,
                        "mitigation": template.mitigation,
                    },
                )
                updates: list[str] = []
                if finding.severity != template.severity:
                    finding.severity = template.severity
                    updates.append("severity")
                if finding.cwe != template.cwe:
                    finding.cwe = template.cwe
                    updates.append("cwe")
                if finding.date != finding_date:
                    finding.date = finding_date
                    updates.append("date")
                if finding.reporter_id != reporter.id:
                    finding.reporter = reporter
                    updates.append("reporter")
                if finding.file_path != template.file_path:
                    finding.file_path = template.file_path
                    updates.append("file_path")
                if finding.vuln_id_from_tool != desired_vuln_id:
                    finding.vuln_id_from_tool = desired_vuln_id
                    updates.append("vuln_id_from_tool")
                if finding.description != template.description:
                    finding.description = template.description
                    updates.append("description")
                if finding.mitigation != template.mitigation:
                    finding.mitigation = template.mitigation
                    updates.append("mitigation")
                if updates:
                    finding.save(update_fields=updates)
                finding_ids.append(finding.id)
                sequence += 1
        return finding_ids

    def _ensure_dast_demo_finding(
        self,
        *,
        spec: DemoProjectSpec,
        project: AISTProject,
        engagement: Engagement,
        reporter,
        base_date,
        sequence: int,
    ) -> tuple[int, Test]:
        """Seed one DAST finding attached to a distinct autonomous scan test."""
        test_type, _ = Test_Type.objects.get_or_create(name=DAST_DEMO_SCAN_TYPE)
        now = timezone.now()
        dast_test, _ = Test.objects.get_or_create(
            engagement=engagement,
            test_type=test_type,
            title=f"{spec.slug} dast autonomous run {sequence:02d}",
            defaults={"target_start": now - timedelta(days=1), "target_end": now},
        )

        # The DAST prefix keeps the SAST distribution fixture stable.
        title = f"Cross-tenant BOLA on subscription keys [DAST-{spec.slug.upper()}-{sequence:03d}]"
        desired = {
            "severity": "High",
            "date": base_date,
            "reporter": reporter,
            "dynamic_finding": True,
            "static_finding": False,
            "unique_id_from_tool": f"{spec.slug}-dast-bola-{sequence:03d}",
            "vuln_id_from_tool": f"{spec.slug}-dast-bola-{sequence:03d}",
            "description": (
                "An authenticated user of one tenant can access another tenant's "
                "subscription resource by guessing the numeric subscription id in the "
                "URL path."
            ),
            "mitigation": (
                "Enforce per-tenant authorization checks on the subscription lookup "
                "instead of relying on the id alone."
            ),
            "steps_to_reproduce": (
                "1. Authenticate as a user in tenant A.\n"
                "2. Request /v1/subscriptions/{tenant B's id}.\n"
                "3. Observe tenant B's subscription data is returned."
            ),
            "param": "subscription_id",
            "payload": "123",
            "cvssv3": "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N",
            "cvssv3_score": 6.5,
            "references": "https://dast-triage.internal/demo/cross-tenant-bola.html",
        }
        finding, _ = Finding.objects.get_or_create(
            test=dast_test,
            unique_id_from_tool=desired["unique_id_from_tool"],
            defaults={"title": title, **desired},
        )
        updates = []
        for field, value in desired.items():
            current = getattr(finding, f"{field}_id") if field == "reporter" else getattr(finding, field)
            target = value.id if field == "reporter" else value
            if current != target:
                setattr(finding, field, value)
                updates.append(field)
        if updates:
            finding.save(update_fields=updates)

        endpoint, _ = endpoint_get_or_create(
            protocol="https",
            host="api.example.com",
            path="v1/subscriptions/123",
            product=project.product,
        )
        Endpoint_Status.objects.get_or_create(
            finding=finding,
            endpoint=endpoint,
            defaults={"date": finding.date},
        )
        return finding.id, dast_test

    @staticmethod
    def _upsert_demo_pipeline(
        *,
        pipeline_id: str,
        project: AISTProject,
        project_version: AISTProjectVersion | None,
        trigger_project_version: AISTProjectVersion | None,
        execution_type: str,
        started,
        finished_at,
        launch_data: dict,
        dojo_test: Test,
    ) -> AISTPipeline:
        pipeline, _ = AISTPipeline.objects.update_or_create(
            id=pipeline_id,
            defaults={
                "project": project,
                "project_version": project_version,
                "trigger_project_version": trigger_project_version,
                "execution_type": execution_type,
                "status": AISTStatus.FINISHED,
                "started": started,
                "finished_at": finished_at,
                "launch_data": launch_data,
                "created": started,
            },
        )
        pipeline.tests.set([dojo_test])
        AISTPipeline.objects.filter(pk=pipeline.pk).update(created=started, updated=finished_at)
        return pipeline

    def _ensure_dast_run_metadata(
        self,
        *,
        pipeline: AISTPipeline,
        binding: DastProjectBinding,
        provider_run_id: str,
        sequence: int,
        started,
        finished_at,
    ) -> None:
        """
        Give the demo run the same reported metadata an accepted report would carry.

        Written through the one writer the real import paths use, so the demo data can only ever
        have the shape production data has.
        """
        index = (sequence - 1) % DAST_DEMO_RUN_COUNT
        discovered, reachable, analysed, planned = DAST_DEMO_COVERAGE[index]
        analysed_names = demo_coverage_names(analysed)
        totals = DAST_DEMO_TOKEN_TOTALS[index]
        total = DastTokenBucket(
            input_tokens=totals[0],
            output_tokens=totals[1],
            thinking_tokens=totals[2],
            cache_creation_tokens=totals[3],
            cache_read_tokens=totals[4],
            calls=totals[5],
        )
        phase_splits = [
            _split_exactly(counter, tuple(weight for _key, _name, weight in DAST_DEMO_PHASES))
            for counter in totals
        ]
        by_phase = tuple(
            DastTokenBucket(
                key=key,
                name=name,
                input_tokens=phase_splits[0][position],
                output_tokens=phase_splits[1][position],
                thinking_tokens=phase_splits[2][position],
                cache_creation_tokens=phase_splits[3][position],
                cache_read_tokens=phase_splits[4][position],
                calls=phase_splits[5][position],
            )
            for position, (key, name, _weight) in enumerate(DAST_DEMO_PHASES)
        )
        agent_splits = [
            _split_exactly(counter, tuple(weight for _key, _agents, weight in DAST_DEMO_AGENT_TYPES))
            for counter in totals
        ]
        by_agent_type = tuple(
            DastTokenBucket(
                key=key,
                agents=agents,
                input_tokens=agent_splits[0][position],
                output_tokens=agent_splits[1][position],
                thinking_tokens=agent_splits[2][position],
                cache_creation_tokens=agent_splits[3][position],
                cache_read_tokens=agent_splits[4][position],
                calls=agent_splits[5][position],
            )
            for position, (key, agents, _weight) in enumerate(DAST_DEMO_AGENT_TYPES)
        )
        DastRunMetadata.objects.upsert_from_report(
            pipeline_id=pipeline.pk,
            metadata=ValidatedDastRunMetadata(
                run_id=provider_run_id,
                target_id=binding.target.provider_id,
                stand_id=f"{binding.target.provider_id}-stand-{sequence:02d}",
                product_family=binding.target.provider_id,
                tier="external",
                run_type="deep" if analysed > planned else "baseline",
                target_host=analysed_names[0] if analysed_names else None,
                scan_started=started,
                scan_finished=finished_at,
                coverage=DastCoverage(
                    unit="endpoint",
                    discovered=discovered,
                    reachable=reachable,
                    analysed=analysed,
                    planned=planned,
                    analysed_names=analysed_names,
                    # Everything past the plan; empty when the run stayed inside it, which is a
                    # reported empty rather than an absence.
                    beyond_plan_names=analysed_names[planned:],
                ),
                token_usage=DastTokenUsage(
                    total=total,
                    by_phase=by_phase,
                    by_agent_type=by_agent_type,
                    # True by construction: _split_exactly guarantees both breakdowns add back up.
                    accounting_consistent=True,
                ),
            ),
        )

    def _ensure_historical_dast_runs(
        self,
        *,
        spec: DemoProjectSpec,
        project: AISTProject,
        main_version: AISTProjectVersion,
        release_version: AISTProjectVersion,
        bindings: list[DastProjectBinding],
        launch_configs: list[AISTProjectLaunchConfig],
        engagement: Engagement,
        reporter,
        now,
        base_date,
    ) -> None:
        day_offsets = (12, 7, 2)
        for sequence, day_offset in enumerate(day_offsets, start=1):
            binding_index = (sequence - 1) % len(bindings)
            binding = bindings[binding_index]
            launch_config = launch_configs[binding_index]
            project_version = release_version if sequence % 2 == 0 else main_version
            requires_source = binding.requires_source_repository
            pipeline_version = project_version if requires_source else None
            pipeline_trigger_version = project_version if requires_source else None
            started = now - timedelta(days=day_offset, minutes=sequence * 11)
            finished_at = started + timedelta(minutes=18 + sequence * 4)
            finding_id, dojo_test = self._ensure_dast_demo_finding(
                spec=spec,
                project=project,
                engagement=engagement,
                reporter=reporter,
                base_date=base_date - timedelta(days=day_offset),
                sequence=sequence,
            )
            dojo_test.target_start = started
            dojo_test.target_end = finished_at
            dojo_test.save(update_fields=["target_start", "target_end"])
            project_version.findings.add(finding_id)

            provider_run_id = f"demo-provider-{spec.slug}-{sequence:02d}"
            pipeline = self._upsert_demo_pipeline(
                pipeline_id=f"demo-{spec.slug}-dast-run-{sequence:02d}",
                project=project,
                project_version=pipeline_version,
                trigger_project_version=pipeline_trigger_version,
                execution_type=PipelineExecutionType.DAST,
                started=started,
                finished_at=finished_at,
                launch_data={
                    "source": "bootstrap_demo_access",
                    "dast_binding_id": binding.pk,
                    "provider_run_id": provider_run_id,
                    "target_id": binding.target.provider_id,
                    "dast_outcome": {
                        "version": "1",
                        "code": "SUCCESS_WITH_FINDINGS",
                    },
                },
                dojo_test=dojo_test,
            )
            DastExecutionState.objects.update_or_create(
                pipeline=pipeline,
                defaults={
                    "run_id": provider_run_id,
                    "log_cursor": 120 + sequence,
                    "outcome": DastExecutionOutcome.TERMINAL,
                    "deadline": finished_at,
                    "recovery_checkpoint": {
                        "source": "bootstrap_demo_access",
                        "terminal": True,
                    },
                },
            )
            self._ensure_dast_run_metadata(
                pipeline=pipeline,
                binding=binding,
                provider_run_id=provider_run_id,
                sequence=sequence,
                started=started,
                finished_at=finished_at,
            )
            PipelineLaunchRequest.objects.update_or_create(
                pipeline=pipeline,
                defaults={
                    "execution_type": PipelineExecutionType.DAST,
                    "project": project,
                    "dast_binding": binding,
                    "trigger_project_version": pipeline_trigger_version,
                    "schedule": launch_config.get_launch_schedule(),
                    "launch_config": launch_config,
                    "origin": PipelineLaunchOrigin.SCHEDULE,
                    "authority_kind": PipelineLaunchAuthorityKind.SCHEDULE,
                    "requester": None,
                    "params_snapshot": dict(binding.parameter_snapshot),
                    "capability_snapshot": {},
                    "initial_launch_data_snapshot": {
                        "source": "bootstrap_demo_access",
                        "dast_binding_id": binding.pk,
                    },
                    "state": PipelineLaunchRequestState.DISPATCHED,
                    "dispatched_at": started,
                },
            )

    def _ensure_historical_manual_imports(
        self,
        *,
        spec: DemoProjectSpec,
        project: AISTProject,
        main_version: AISTProjectVersion,
        release_version: AISTProjectVersion,
        engagement: Engagement,
        reporter,
        now,
        base_date,
    ) -> None:
        test_type, _ = Test_Type.objects.get_or_create(name=MANUAL_DEMO_SCAN_TYPE)
        day_offsets = (10, 5, 1)
        for sequence, day_offset in enumerate(day_offsets, start=1):
            project_version = release_version if sequence % 2 == 0 else main_version
            started = now - timedelta(days=day_offset, minutes=sequence * 7)
            finished_at = started + timedelta(minutes=2 + sequence)
            dojo_test, _ = Test.objects.get_or_create(
                engagement=engagement,
                test_type=test_type,
                title=f"{spec.slug} manual report upload {sequence:02d}",
                defaults={"target_start": started, "target_end": finished_at},
            )
            dojo_test.target_start = started
            dojo_test.target_end = finished_at
            dojo_test.save(update_fields=["target_start", "target_end"])

            template = DEMO_FINDING_TEMPLATES[(sequence - 1) % len(DEMO_FINDING_TEMPLATES)]
            title = f"Manual report: {template.title} [MANUAL-{spec.slug.upper()}-{sequence:03d}]"
            unique_id_from_tool = f"{spec.slug}-manual-{sequence:03d}"
            finding, _ = Finding.objects.get_or_create(
                test=dojo_test,
                unique_id_from_tool=unique_id_from_tool,
                defaults={
                    "title": title,
                    "severity": template.severity,
                    "cwe": template.cwe,
                    "date": base_date - timedelta(days=day_offset),
                    "reporter": reporter,
                    "file_path": template.file_path,
                    "static_finding": True,
                    "dynamic_finding": False,
                    "vuln_id_from_tool": f"{template.vuln_id}-MANUAL-{sequence:03d}",
                    "description": f"Imported by an operator from {MANUAL_DEMO_SCAN_TYPE}. {template.description}",
                    "mitigation": template.mitigation,
                },
            )
            project_version.findings.add(finding.pk)
            filename = f"{spec.slug}-manual-report-{sequence:02d}.json"
            sha256 = hashlib.sha256(filename.encode()).hexdigest()
            self._upsert_demo_pipeline(
                pipeline_id=f"demo-{spec.slug}-manual-import-{sequence:02d}",
                project=project,
                project_version=project_version,
                trigger_project_version=None,
                execution_type=PipelineExecutionType.MANUAL_IMPORT,
                started=started,
                finished_at=finished_at,
                launch_data={
                    "source": "manual_import",
                    "scan_type": MANUAL_DEMO_SCAN_TYPE,
                    "uploader_id": reporter.pk,
                    "filename": filename,
                    "sha256": sha256,
                },
                dojo_test=dojo_test,
            )

    def _ensure_historical_queue(
            self,
            *,
            spec: DemoProjectSpec,
            project: AISTProject,
            main_version: AISTProjectVersion,
            release_version: AISTProjectVersion,
            launch_config: AISTProjectLaunchConfig,
            schedule: LaunchSchedule,
            now,
    ) -> None:
        for index, day_offset in enumerate(spec.queue_day_offsets, start=1):
            run_timestamp = now - timedelta(days=day_offset, minutes=index * 3)
            status = AISTStatus.FINISHED if index % 3 else AISTStatus.FINISHED_WITH_WARNINGS
            project_version = release_version if index % 2 == 0 else main_version
            duration_minutes = 0 if index % 5 == 0 else (30 if index % 2 else 5)
            pipeline_finished_at = run_timestamp + timedelta(minutes=duration_minutes)
            pipeline_id = f"demo-{spec.slug}-run-{index:02d}"
            pipeline, _ = AISTPipeline.objects.get_or_create(
                id=pipeline_id,
                defaults={
                    "project": project,
                    "project_version": project_version,
                    "execution_type": PipelineExecutionType.SAST,
                    "status": status,
                    "started": run_timestamp,
                    "finished_at": pipeline_finished_at,
                    "launch_data": {"source": "bootstrap_demo_access", "sequence": index},
                    "created": run_timestamp,
                },
            )
            pipeline_updates: list[str] = []
            if pipeline.project_id != project.id:
                pipeline.project = project
                pipeline_updates.append("project")
            if pipeline.project_version_id != project_version.id:
                pipeline.project_version = project_version
                pipeline_updates.append("project_version")
            if pipeline.execution_type != PipelineExecutionType.SAST:
                pipeline.execution_type = PipelineExecutionType.SAST
                pipeline_updates.append("execution_type")
            if pipeline.trigger_project_version_id is not None:
                pipeline.trigger_project_version = None
                pipeline_updates.append("trigger_project_version")
            if pipeline.status != status:
                pipeline.status = status
                pipeline_updates.append("status")
            if pipeline.started != run_timestamp:
                pipeline.started = run_timestamp
                pipeline_updates.append("started")
            if pipeline.finished_at != pipeline_finished_at:
                pipeline.finished_at = pipeline_finished_at
                pipeline_updates.append("finished_at")
            if pipeline_updates:
                pipeline.save(update_fields=pipeline_updates)
            AISTPipeline.objects.filter(pk=pipeline.pk).update(created=run_timestamp, updated=pipeline_finished_at)

            dispatched_at = run_timestamp + timedelta(minutes=7)
            queue_item, _ = PipelineLaunchRequest.objects.get_or_create(
                pipeline=pipeline,
                defaults={
                    "execution_type": PipelineExecutionType.SAST,
                    "project": project,
                    "schedule": schedule,
                    "launch_config": launch_config,
                    "origin": PipelineLaunchOrigin.SCHEDULE,
                    "authority_kind": PipelineLaunchAuthorityKind.SCHEDULE,
                    "params_snapshot": dict(launch_config.params),
                    "state": PipelineLaunchRequestState.DISPATCHED,
                    "dispatched_at": dispatched_at,
                },
            )
            queue_updates: list[str] = []
            if queue_item.project_id != project.id:
                queue_item.project = project
                queue_updates.append("project")
            if queue_item.schedule_id != schedule.id:
                queue_item.schedule = schedule
                queue_updates.append("schedule")
            if queue_item.launch_config_id != launch_config.id:
                queue_item.launch_config = launch_config
                queue_updates.append("launch_config")
            if queue_item.execution_type != PipelineExecutionType.SAST:
                queue_item.execution_type = PipelineExecutionType.SAST
                queue_updates.append("execution_type")
            if queue_item.dast_binding_id is not None:
                queue_item.dast_binding = None
                queue_updates.append("dast_binding")
            if queue_item.trigger_project_version_id is not None:
                queue_item.trigger_project_version = None
                queue_updates.append("trigger_project_version")
            if queue_item.state != PipelineLaunchRequestState.DISPATCHED:
                queue_item.state = PipelineLaunchRequestState.DISPATCHED
                queue_updates.append("state")
            if queue_item.dispatched_at != dispatched_at:
                queue_item.dispatched_at = dispatched_at
                queue_updates.append("dispatched_at")
            if queue_updates:
                queue_item.save(update_fields=queue_updates)
            PipelineLaunchRequest.objects.filter(pk=queue_item.pk).update(created=run_timestamp)

    def _ensure_demo_ai_responses(self, *, project: AISTProject) -> None:
        latest_pipeline = (
            AISTPipeline.objects.filter(project=project)
            .order_by("-created", "-updated", "-id")
            .first()
        )
        if latest_pipeline is None:
            return

        findings = list(
            Finding.objects
            .filter(test__engagement__product=project.product)
            .order_by("id"),
        )
        if not findings:
            return

        assigned = [
            (finding, DEMO_AI_PROFILES[index % len(DEMO_AI_PROFILES)])
            for index, finding in enumerate(findings)
        ]

        payload_results = {
            "true_positives": [],
            "false_positives": [],
            "uncertain": [],
        }
        for finding, profile in assigned:
            entry = {
                "title": f"{profile.label}: {finding.title}",
                "reasoning": profile.reasoning,
                "originalFinding": {"id": finding.id},
                "references": list(profile.references),
                "epssScore": profile.epss_score,
                "impactScore": profile.impact_score,
                "exploitabilityScore": profile.exploitability_score,
                "uncertaintyLevel": profile.uncertainty_level,
                "uncertaintySpread": profile.uncertainty_spread,
                "exploitCodeMaturity": profile.exploit_code_maturity,
                "fix": profile.fix,
            }
            if profile.verdict == AISTAIFindingResponse.Verdict.TRUE_POSITIVE:
                payload_results["true_positives"].append(entry)
            elif profile.verdict == AISTAIFindingResponse.Verdict.FALSE_POSITIVE:
                payload_results["false_positives"].append(entry)
            else:
                payload_results["uncertain"].append(entry)

        payload = {
            "generated_by": "bootstrap_demo_access",
            "profile_version": 1,
            "results": payload_results,
        }
        source_response = (
            AISTAIResponse.objects
            .filter(pipeline=latest_pipeline, payload__generated_by="bootstrap_demo_access")
            .order_by("-created")
            .first()
        )
        if source_response is None:
            source_response = AISTAIResponse.objects.create(
                pipeline=latest_pipeline,
                payload=payload,
            )
        else:
            source_response.payload = payload
            source_response.save(update_fields=["payload"])

        for finding, profile in assigned:
            AISTAIFindingResponse.objects.update_or_create(
                pipeline=latest_pipeline,
                finding=finding,
                defaults={
                    "source_response": source_response,
                    "verdict": profile.verdict,
                    "title": f"{profile.label}: {finding.title}"[:512],
                    "summary": profile.reasoning,
                    "references": list(profile.references),
                    "epss_score": profile.epss_score,
                    "impact_score": profile.impact_score,
                    "exploitability_score": profile.exploitability_score,
                    "uncertainty_level": profile.uncertainty_level,
                    "uncertainty_spread": profile.uncertainty_spread,
                    "exploit_code_maturity": profile.exploit_code_maturity,
                    "fix": profile.fix,
                },
            )
