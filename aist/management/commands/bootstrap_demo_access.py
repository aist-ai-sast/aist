from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from dojo.models import (
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
    LaunchSchedule,
    Organization,
    PipelineLaunchQueue,
    VersionType,
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

    def _ensure_demo_projects(self, *, organizations: list[Organization], users_by_username: dict[str, object]) -> None:
        organizations_by_name = {org.name: org for org in organizations}
        first_maintainer_username = next(s.username for s in DEMO_USERS if s.role_name == "Maintainer")
        default_reporter = users_by_username[first_maintainer_username]
        now = timezone.now()
        today = timezone.localdate()
        sla_config, _ = SLA_Configuration.objects.get_or_create(name="Demo SLA")
        test_type, _ = Test_Type.objects.get_or_create(name="Semgrep JSON Report")

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
                    "organization": organization,
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
            if project.organization_id != organization.id:
                project.organization = organization
                project_updates.append("organization")
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

            launch_config, _ = AISTProjectLaunchConfig.objects.get_or_create(
                project=project,
                name=spec.launch_config_name,
                defaults={
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
                    "max_concurrent_per_worker": 1,
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
            if schedule.max_concurrent_per_worker != 1:
                schedule.max_concurrent_per_worker = 1
                schedule_updates.append("max_concurrent_per_worker")
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
            self._ensure_demo_ai_responses(project=project)

    def _ensure_project_findings(self, *, spec: DemoProjectSpec, dojo_test: Test, reporter, base_date):
        finding_ids: list[int] = []
        sequence = 1
        for day_offset, findings_count in zip(spec.finding_day_offsets, spec.finding_distribution, strict=True):
            finding_date = base_date - timedelta(days=day_offset)
            for _ in range(findings_count):
                template = DEMO_FINDING_TEMPLATES[(sequence - 1) % len(DEMO_FINDING_TEMPLATES)]
                title = f"{template.title} [{spec.slug.upper()}-{sequence:03d}]"
                finding, _ = Finding.objects.get_or_create(
                    test=dojo_test,
                    title=title,
                    defaults={
                        "severity": template.severity,
                        "cwe": template.cwe,
                        "date": finding_date,
                        "reporter": reporter,
                        "file_path": template.file_path,
                        "vuln_id_from_tool": f"{template.vuln_id}-{sequence:03d}",
                        "description": template.description,
                        "mitigation": template.mitigation,
                    },
                )
                updates: list[str] = []
                desired_vuln_id = f"{template.vuln_id}-{sequence:03d}"
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
                    "status": status,
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
            if pipeline.status != status:
                pipeline.status = status
                pipeline_updates.append("status")
            if pipeline_updates:
                pipeline.save(update_fields=pipeline_updates)
            AISTPipeline.objects.filter(pk=pipeline.pk).update(created=run_timestamp, updated=pipeline_finished_at)

            is_dispatched = index % 4 != 0
            dispatched_at = run_timestamp + timedelta(minutes=7) if is_dispatched else None
            queue_item, _ = PipelineLaunchQueue.objects.get_or_create(
                pipeline=pipeline,
                defaults={
                    "project": project,
                    "schedule": schedule,
                    "launch_config": launch_config,
                    "dispatched": is_dispatched,
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
            if queue_item.dispatched != is_dispatched:
                queue_item.dispatched = is_dispatched
                queue_updates.append("dispatched")
            if queue_item.dispatched_at != dispatched_at:
                queue_item.dispatched_at = dispatched_at
                queue_updates.append("dispatched_at")
            if queue_updates:
                queue_item.save(update_fields=queue_updates)
            PipelineLaunchQueue.objects.filter(pk=queue_item.pk).update(created=run_timestamp)

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
