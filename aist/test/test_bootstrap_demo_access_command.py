from __future__ import annotations

from collections import Counter
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from dojo.models import Finding, Role

from aist.management.commands.bootstrap_demo_access import DEMO_PROJECTS, DEMO_USERS
from aist.models import (
    AISTPipeline,
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    LaunchSchedule,
    Organization,
    PipelineLaunchQueue,
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

        projects = AISTProject.objects.filter(
            product__name__in=[spec.product_name for spec in DEMO_PROJECTS],
        ).select_related("organization", "product", "product__prod_type")
        self.assertEqual(projects.count(), len(DEMO_PROJECTS))

        org_project_counts = Counter(
            projects.values_list("organization__name", flat=True),
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

            launch_config = AISTProjectLaunchConfig.objects.get(
                project=project,
                name=spec.launch_config_name,
            )
            schedule = LaunchSchedule.objects.get(launch_config=launch_config)
            self.assertEqual(schedule.cron_expression, spec.cron_expression)
            self.assertIsNotNone(schedule.last_run_at)

            queue_qs = PipelineLaunchQueue.objects.filter(
                project=project,
                launch_config=launch_config,
            )
            self.assertEqual(queue_qs.count(), len(spec.queue_day_offsets))
            self.assertTrue(queue_qs.filter(dispatched=True).exists())
            self.assertTrue(queue_qs.filter(dispatched=False).exists())
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
