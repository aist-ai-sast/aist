from __future__ import annotations

from collections import Counter
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from dojo.models import Finding, Product_Type_Member, Role

from aist.management.commands.bootstrap_demo_access import DEMO_PROJECTS, DEMO_USERS, ORG_NAMES
from aist.models import (
    AISTPipeline,
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    LaunchSchedule,
    Organization,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)


class BootstrapDemoAccessCommandTests(TestCase):
    def setUp(self):
        self.password = "DemoPassword123!"  # noqa: S105
        get_user_model().objects.create_superuser(
            username="admin-bootstrap",
            email="admin-bootstrap@example.local",
            password="InitialPassword123!",  # noqa: S106
        )
        for idx, spec in enumerate(DEMO_USERS, start=100):
            Role.objects.get_or_create(name=spec.role_name, defaults={"id": idx})

    def test_command_creates_demo_projects_findings_schedules_and_queue_history(self):
        call_command("bootstrap_demo_access", "--skip-admin", "--password", self.password)

        self.assertFalse(Organization.objects.filter(name__in=ORG_NAMES, product_type__isnull=True).exists())

        projects = AISTProject.objects.filter(
            product__name__in=[spec.product_name for spec in DEMO_PROJECTS],
        ).select_related("product__prod_type__aist_organization")
        self.assertEqual(projects.count(), len(DEMO_PROJECTS))

        org_project_counts = Counter(
            projects.values_list("product__prod_type__aist_organization__name", flat=True),
        )
        self.assertTrue(any(count > 1 for count in org_project_counts.values()))

        today = timezone.localdate()
        for spec in DEMO_PROJECTS:
            project = AISTProject.objects.get(product__name=spec.product_name)
            organization = Organization.objects.get(name=spec.organization_name)
            self.assertEqual(project.organization_id, organization.id)
            self.assertEqual(project.product.prod_type_id, organization.product_type_id)
            self.assertEqual(project.versions.count(), 2)

            findings_qs = Finding.objects.filter(
                test__engagement__product=project.product,
                title__contains=f"[{spec.slug.upper()}-",
            )
            self.assertEqual(findings_qs.count(), sum(spec.finding_distribution))
            self.assertEqual(findings_qs.values("date").distinct().count(), len(spec.finding_distribution))
            self.assertFalse(findings_qs.filter(cwe__isnull=True).exists())
            self.assertFalse(findings_qs.filter(cwe=0).exists())
            linked_findings_count = AISTProjectVersion.findings.through.objects.filter(
                aistprojectversion__project=project,
                finding_id__in=findings_qs.values("id"),
            ).values("finding_id").distinct().count()
            self.assertEqual(linked_findings_count, findings_qs.count())

            expected_by_day = Counter({
                today - timedelta(days=day_offset): count
                for day_offset, count in zip(spec.finding_day_offsets, spec.finding_distribution, strict=True)
            })
            actual_by_day = Counter(findings_qs.values_list("date", flat=True))
            self.assertEqual(actual_by_day, expected_by_day)

            dast_finding = Finding.objects.get(
                test__engagement__product=project.product,
                title__contains=f"[DAST-{spec.slug.upper()}-",
            )
            self.assertTrue(dast_finding.dynamic_finding)
            self.assertEqual(
                dast_finding.references,
                "https://dast-triage.internal/demo/cross-tenant-bola.html",
            )
            self.assertEqual(dast_finding.endpoints.count(), 1)

            launch_config = AISTProjectLaunchConfig.objects.get(
                project=project,
                name=spec.launch_config_name,
            )
            self.assertEqual(launch_config.params.get("ai_mode"), "AUTO_DEFAULT")
            self.assertEqual(
                launch_config.params.get("ai_filter_snapshot"),
                {
                    "limit": 50,
                    "severity": [{"comparison": "EQUALS", "value": "HIGH"}],
                },
            )
            schedule = LaunchSchedule.objects.get(launch_config=launch_config)
            self.assertEqual(schedule.cron_expression, spec.cron_expression)
            self.assertIsNotNone(schedule.last_run_at)

            queue_qs = PipelineLaunchRequest.objects.filter(
                project=project,
                launch_config=launch_config,
            )
            self.assertEqual(queue_qs.count(), len(spec.queue_day_offsets))
            self.assertTrue(queue_qs.filter(state=PipelineLaunchRequestState.DISPATCHED).exists())
            self.assertTrue(queue_qs.filter(state=PipelineLaunchRequestState.PENDING).exists())
            self.assertTrue(queue_qs.filter(created__date__lt=today).exists())

            pipeline_qs = AISTPipeline.objects.filter(
                project=project,
                id__startswith=f"demo-{spec.slug}-run-",
            )
            self.assertEqual(pipeline_qs.count(), len(spec.queue_day_offsets))
            self.assertTrue(pipeline_qs.filter(project_version__version="main").exists())
            self.assertTrue(pipeline_qs.filter(project_version__version="release-v1").exists())
            self.assertTrue(pipeline_qs.filter(created__date__lt=today).exists())
            durations = [int((pipeline.updated - pipeline.created).total_seconds()) for pipeline in pipeline_qs]
            self.assertTrue(all(duration >= 0 for duration in durations))
            self.assertIn(0, durations)
            self.assertIn(5 * 60, durations)
            self.assertIn(30 * 60, durations)

    def test_each_demo_user_belongs_to_exactly_their_assigned_organization(self):
        """
        Each demo user must have membership in exactly one org — the one from DEMO_USERS spec.
        Regression: users_by_role was keyed by role_name (non-unique), causing users with the
        same role name (e.g. multiple Maintainers) to overwrite each other and end up assigned
        to all organizations instead of only their own.
        """
        call_command("bootstrap_demo_access", "--skip-admin", "--password", self.password)

        User = get_user_model()
        org_by_name = {org.name: org for org in Organization.objects.filter(name__in=ORG_NAMES)}

        for spec in DEMO_USERS:
            user = User.objects.get(username=spec.username)
            expected_org = org_by_name[spec.organization_name]
            memberships = Product_Type_Member.objects.filter(
                user=user,
                product_type_id__in=[o.product_type_id for o in org_by_name.values() if o.product_type_id],
            ).select_related("product_type")

            self.assertEqual(
                memberships.count(),
                1,
                f"{spec.username} should have exactly 1 org membership, "
                f"got: {[m.product_type_id for m in memberships]}",
            )
            self.assertEqual(
                memberships.first().product_type_id,
                expected_org.product_type_id,
                f"{spec.username} should be in '{spec.organization_name}' only",
            )
