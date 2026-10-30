from __future__ import annotations

from collections import Counter
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone
from dojo.models import Finding, Product_Type_Member, Role, System_Settings

from aist.integrations.dast_queue_contract import normalise_descriptor, normalise_page
from aist.management.commands.bootstrap_demo_access import (
    DAST_DEMO_COVERAGE,
    DAST_DEMO_RUN_COUNT,
    DEMO_PROJECTS,
    DEMO_USERS,
    ORG_NAMES,
)
from aist.models import (
    AISTPipeline,
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    DastProjectBinding,
    DastQueueSnapshot,
    DastRunMetadata,
    LaunchSchedule,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    PipelineExecutionType,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)
from aist.services.dast_queue import queue_is_addressable


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
        call_command("bootstrap_demo_access", "--demo", "--skip-admin", "--password", self.password)

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

            dast_findings = Finding.objects.filter(
                test__engagement__product=project.product,
                title__contains=f"[DAST-{spec.slug.upper()}-",
            )
            self.assertEqual(dast_findings.count(), 3)
            dast_finding = dast_findings.order_by("id").first()
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
            self.assertEqual(launch_config.execution_type, PipelineExecutionType.SAST)
            self.assertIsNone(launch_config.dast_binding_id)
            self.assertIsNone(launch_config.trigger_project_version_id)
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
            self.assertIsNotNone(schedule.next_run_at)
            self.assertGreater(schedule.next_run_at, timezone.now())

            queue_qs = PipelineLaunchRequest.objects.filter(
                project=project,
                launch_config=launch_config,
            )
            self.assertEqual(queue_qs.count(), len(spec.queue_day_offsets))
            self.assertFalse(queue_qs.exclude(execution_type=PipelineExecutionType.SAST).exists())
            self.assertFalse(queue_qs.exclude(state=PipelineLaunchRequestState.DISPATCHED).exists())
            self.assertTrue(queue_qs.filter(created__date__lt=today).exists())

            pipeline_qs = AISTPipeline.objects.filter(
                project=project,
                id__startswith=f"demo-{spec.slug}-run-",
            )
            self.assertEqual(pipeline_qs.count(), len(spec.queue_day_offsets))
            self.assertTrue(pipeline_qs.filter(project_version__version="main").exists())
            self.assertTrue(pipeline_qs.filter(project_version__version="release-v1").exists())
            self.assertTrue(pipeline_qs.filter(created__date__lt=today).exists())
            self.assertFalse(pipeline_qs.exclude(execution_type=PipelineExecutionType.SAST).exists())
            self.assertFalse(pipeline_qs.filter(started__isnull=True).exists())
            self.assertFalse(pipeline_qs.filter(finished_at__isnull=True).exists())
            durations = [
                int((pipeline.finished_at - pipeline.started).total_seconds())
                for pipeline in pipeline_qs
            ]
            self.assertTrue(all(duration >= 0 for duration in durations))
            self.assertIn(0, durations)
            self.assertIn(5 * 60, durations)
            self.assertIn(30 * 60, durations)

            dast_bindings = DastProjectBinding.objects.filter(project=project)
            self.assertGreaterEqual(dast_bindings.count(), 1)
            self.assertFalse(dast_bindings.filter(enabled=False).exists())
            # Demo target parameters must mirror the real DAST provider contract (a single
            # "depth" enum field, default "light") rather than inventing schema fields that
            # don't exist in the actual product.
            for binding in dast_bindings:
                self.assertEqual(
                    set(binding.target.parameter_schema["properties"]),
                    {"depth"},
                )
                self.assertEqual(binding.target.provider_defaults, {"depth": "light"})

            dast_configs = AISTProjectLaunchConfig.objects.filter(
                project=project,
                execution_type=PipelineExecutionType.DAST,
            )
            self.assertEqual(dast_configs.count(), dast_bindings.count())
            self.assertFalse(dast_configs.filter(dast_binding__isnull=True).exists())
            # Exactly the bindings whose target has no repository requirement (the demo
            # "perimeter" target) go without a trigger version; every other binding must have one.
            for config in dast_configs:
                self.assertEqual(
                    config.trigger_project_version_id is None,
                    not config.dast_binding.requires_source_repository,
                )
            self.assertTrue(dast_configs.filter(trigger_project_version__isnull=True).exists())
            self.assertTrue(dast_configs.filter(trigger_project_version__isnull=False).exists())

            dast_pipelines = AISTPipeline.objects.filter(
                project=project,
                id__startswith=f"demo-{spec.slug}-dast-run-",
            )
            self.assertEqual(dast_pipelines.count(), 3)
            self.assertFalse(dast_pipelines.exclude(execution_type=PipelineExecutionType.DAST).exists())
            self.assertTrue(all(pipeline.tests.exists() for pipeline in dast_pipelines))
            self.assertTrue(all(pipeline.launch_data.get("dast_binding_id") for pipeline in dast_pipelines))
            for pipeline in dast_pipelines:
                binding = DastProjectBinding.objects.get(pk=pipeline.launch_data["dast_binding_id"])
                self.assertEqual(
                    pipeline.trigger_project_version_id is None,
                    not binding.requires_source_repository,
                )
            self.assertTrue(dast_pipelines.filter(trigger_project_version__isnull=True).exists())
            self.assertTrue(dast_pipelines.filter(trigger_project_version__isnull=False).exists())

            manual_imports = AISTPipeline.objects.filter(
                project=project,
                id__startswith=f"demo-{spec.slug}-manual-import-",
            )
            self.assertEqual(manual_imports.count(), 3)
            self.assertFalse(manual_imports.exclude(execution_type=PipelineExecutionType.MANUAL_IMPORT).exists())
            self.assertTrue(all(pipeline.tests.exists() for pipeline in manual_imports))
            self.assertTrue(all(pipeline.launch_data.get("source") == "manual_import" for pipeline in manual_imports))

            self.assertTrue(
                Finding.objects.filter(
                    test__aist_pipelines__in=dast_pipelines,
                    title__contains=f"[DAST-{spec.slug.upper()}-",
                ).exists(),
            )
            self.assertTrue(
                Finding.objects.filter(
                    test__aist_pipelines__in=manual_imports,
                    title__contains=f"[MANUAL-{spec.slug.upper()}-",
                ).exists(),
            )

        self.assertTrue(
            any(
                project.dast_bindings.count() > 1
                for project in projects
            ),
        )
        for organization in Organization.objects.filter(name__in=ORG_NAMES):
            self.assertEqual(
                OrgIntegration.objects.filter(
                    organization=organization,
                    integration_type=OrgIntegrationType.DAST,
                ).count(),
                1,
            )

        counts_before_rerun = {
            "bindings": DastProjectBinding.objects.count(),
            "configs": AISTProjectLaunchConfig.objects.count(),
            "pipelines": AISTPipeline.objects.count(),
            "findings": Finding.objects.count(),
        }
        call_command("bootstrap_demo_access", "--demo", "--skip-admin", "--password", self.password)
        self.assertEqual(
            counts_before_rerun,
            {
                "bindings": DastProjectBinding.objects.count(),
                "configs": AISTProjectLaunchConfig.objects.count(),
                "pipelines": AISTPipeline.objects.count(),
                "findings": Finding.objects.count(),
            },
        )

    def test_demo_projects_carry_tags_and_their_findings_inherit_them(self):
        """
        The demo is where this feature is shown, so it has to be visible there.

        A project tag is only useful because it reaches the project's findings —
        that is what makes it a finding filter. Both halves are asserted here:
        the card has the tag, and the findings under it carry the same tag.
        """
        call_command("bootstrap_demo_access", "--demo", "--skip-admin", "--password", self.password)

        for spec in DEMO_PROJECTS:
            with self.subTest(project=spec.slug):
                self.assertTrue(spec.tags, "every demo project is expected to show tags")
                project = AISTProject.objects.get(product__name=spec.product_name)
                self.assertEqual(
                    sorted(tag.name for tag in project.product.tags.all()),
                    sorted(spec.tags),
                )

                findings = Finding.objects.filter(test__engagement__product=project.product)
                self.assertTrue(findings.exists(), "a demo project without findings shows nothing")
                for finding in findings:
                    carried = {tag.name for tag in finding.tags.all()}
                    self.assertTrue(
                        set(spec.tags) <= carried,
                        f"{finding.title} is missing project tags {set(spec.tags) - carried}",
                    )

    def test_a_demo_tag_shared_by_two_organizations_stays_separate_per_project(self):
        """``tier-1`` is deliberately reused across organizations in the demo data."""
        call_command("bootstrap_demo_access", "--demo", "--skip-admin", "--password", self.password)

        sharing = [spec for spec in DEMO_PROJECTS if "tier-1" in spec.tags]
        self.assertGreater(len(sharing), 1, "the demo should show one tag name in several orgs")
        organizations = {spec.organization_name for spec in sharing}
        self.assertGreater(len(organizations), 1, "and in more than one organization")

        for spec in sharing:
            project = AISTProject.objects.get(product__name=spec.product_name)
            self.assertIn("tier-1", [tag.name for tag in project.product.tags.all()])

    def test_each_demo_user_belongs_to_exactly_their_assigned_organization(self):
        """
        Each demo user must have membership in exactly one org — the one from DEMO_USERS spec.
        Regression: users_by_role was keyed by role_name (non-unique), causing users with the
        same role name (e.g. multiple Maintainers) to overwrite each other and end up assigned
        to all organizations instead of only their own.
        """
        call_command("bootstrap_demo_access", "--demo", "--skip-admin", "--password", self.password)

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


class BootstrapDemoAccessDastRunMetadataTests(BootstrapDemoAccessCommandTests):

    """
    Demo DAST runs must carry reported run metadata.

    The seeder writes pipelines directly instead of going through report finalization, so without
    this the coverage and spend panels are invisible on every demo run — the one place someone
    looks at them first.
    """

    def test_every_demo_dast_run_carries_coverage_and_spend(self):
        call_command("bootstrap_demo_access", "--demo", "--skip-admin", "--password", self.password)

        dast_pipelines = AISTPipeline.objects.filter(execution_type=PipelineExecutionType.DAST)
        self.assertGreater(dast_pipelines.count(), 0)
        for pipeline in dast_pipelines:
            with self.subTest(pipeline=pipeline.pk):
                row = DastRunMetadata.objects.get(pipeline_id=pipeline.pk)
                self.assertEqual(row.coverage_unit, "endpoint")
                self.assertIsNotNone(row.discovered)
                self.assertEqual(len(row.analysed_names), row.analysed)
                self.assertIsNotNone(row.model_calls)
                self.assertTrue(row.token_accounting_consistent)

    def test_the_breakdowns_add_back_up_to_the_reported_total(self):
        call_command("bootstrap_demo_access", "--demo", "--skip-admin", "--password", self.password)

        counters = ("input_tokens", "output_tokens", "thinking_tokens", "cache_creation_tokens",
                    "cache_read_tokens", "calls")
        for row in DastRunMetadata.objects.exclude(token_by_phase=None):
            for buckets in (row.token_by_phase, row.token_by_agent_type):
                for counter in counters:
                    column = "model_calls" if counter == "calls" else counter
                    with self.subTest(pipeline=row.pipeline_id, counter=counter):
                        self.assertEqual(
                            sum(bucket[counter] for bucket in buckets),
                            getattr(row, column),
                        )

    def test_the_beyond_plan_chip_is_conditional_across_the_demo_runs(self):
        call_command("bootstrap_demo_access", "--demo", "--skip-admin", "--password", self.password)

        # One demo run stays inside its plan, so the amber chip must not be on every row.
        overshoots = {
            len(row.beyond_plan_names) > 0
            for row in DastRunMetadata.objects.exclude(beyond_plan_names=None)
        }
        self.assertEqual(overshoots, {True, False})
        inside_plan = [coverage for coverage in DAST_DEMO_COVERAGE if coverage[2] <= coverage[3]]
        self.assertTrue(inside_plan)
        self.assertEqual(len(DAST_DEMO_COVERAGE), DAST_DEMO_RUN_COUNT)

    def test_rerunning_the_command_leaves_one_metadata_row_per_run(self):
        call_command("bootstrap_demo_access", "--demo", "--skip-admin", "--password", self.password)
        before = DastRunMetadata.objects.count()

        call_command("bootstrap_demo_access", "--demo", "--skip-admin", "--password", self.password)

        self.assertEqual(DastRunMetadata.objects.count(), before)


class BootstrapDemoAccessPlatformSettingsTests(BootstrapDemoAccessCommandTests):

    """What a bootstrapped install is set up to do with the findings it holds."""

    def test_deduplication_is_on_and_keeps_no_duplicates(self):
        call_command("bootstrap_demo_access", "--demo", "--skip-admin", "--password", self.password)

        settings_row = System_Settings.objects.first()
        self.assertTrue(settings_row.enable_deduplication)
        self.assertTrue(settings_row.delete_duplicates)
        self.assertEqual(settings_row.max_dupes, 0)

    def test_the_command_refuses_to_run_without_an_explicit_demo_confirmation(self):
        """It writes global settings and known passwords, so it may not run by accident."""
        with self.assertRaises(CommandError):
            call_command("bootstrap_demo_access", "--skip-admin", "--password", self.password)

        self.assertFalse(Organization.objects.filter(name__in=ORG_NAMES).exists())

    def test_the_bootstrap_turns_project_tag_inheritance_on_itself(self):
        """
        A demo install must not rely on the migration's value still being set.

        With the switch off the demo would show tags on the project cards and
        nothing on the findings underneath, which is the one thing the feature
        is for — so the bootstrap pins it the way it pins deduplication.
        """
        settings_row = System_Settings.objects.first()
        if settings_row is None:
            settings_row = System_Settings.objects.create()
        settings_row.enable_product_tag_inheritance = False
        settings_row.save(update_fields=["enable_product_tag_inheritance"])

        call_command("bootstrap_demo_access", "--demo", "--skip-admin", "--password", self.password)

        settings_row.refresh_from_db()
        self.assertTrue(settings_row.enable_product_tag_inheritance)


class BootstrapDemoAccessQueueSnapshotTests(BootstrapDemoAccessCommandTests):

    """
    The run-queue block on a machine with no gateway.

    A development install cannot reach a DAST gateway, and the page never talks to one anyway: it
    renders the snapshot a worker leaves behind. The bootstrap leaves one of its own, so the whole
    surface is visible locally -- and it has to be shaped like a real payload, or the page would
    render something no gateway would ever send.
    """

    def test_every_finished_demo_dast_run_has_a_queue_to_show(self):
        call_command("bootstrap_demo_access", "--demo", "--skip-admin", "--password", self.password)

        pipelines = AISTPipeline.objects.filter(execution_type=PipelineExecutionType.DAST)
        self.assertGreater(pipelines.count(), 0)
        for pipeline in pipelines:
            snapshot = DastQueueSnapshot.objects.filter(pipeline=pipeline).first()
            self.assertIsNotNone(snapshot, pipeline.id)
            self.assertEqual(snapshot.source, DastQueueSnapshot.SOURCE_BOOTSTRAP)
            self.assertEqual(snapshot.run_id, pipeline.dast_execution_state.run_id)
            self.assertTrue(queue_is_addressable(pipeline))

    def test_the_demo_payload_is_the_shape_a_gateway_would_send(self):
        call_command("bootstrap_demo_access", "--demo", "--skip-admin", "--password", self.password)

        snapshot = DastQueueSnapshot.objects.first()
        # Read exactly as stored: the page is served this row and normalises nothing of its own, so
        # a test that normalised it here would be checking a payload nobody is ever shown.
        descriptor = snapshot.descriptor
        page = snapshot.page
        self.assertEqual(descriptor, normalise_descriptor(descriptor))
        self.assertEqual(page, normalise_page(page))
        self.assertEqual(page["dropped_rows"], 0)
        self.assertTrue(page["mutations_frozen"])
        self.assertEqual(page["run_state"], "succeeded")
        self.assertEqual(
            sum(page["counts"]["by_status"].values()),
            len(page["items"]),
        )
        declared = {entry["id"] for entry in descriptor["vocabulary"]["status"]}
        self.assertTrue({item["status"] for item in page["items"]} <= declared)

    def test_a_finished_run_offers_only_the_pause_it_still_holds(self):
        call_command("bootstrap_demo_access", "--demo", "--skip-admin", "--password", self.password)

        page = DastQueueSnapshot.objects.first().page
        offered = [action["name"] for item in page["items"] for action in item["actions"]]
        self.assertEqual(offered, ["resume"])

    def test_the_one_control_a_finished_demo_run_offers_can_actually_be_filled_in(self):
        # Whether a form field can be rendered is decided when the payload is stored. A demo
        # written straight to the row carries no such verdict, and the page then shows its only
        # control read-only -- the whole surface, demonstrably unusable.
        call_command("bootstrap_demo_access", "--demo", "--skip-admin", "--password", self.password)

        page = DastQueueSnapshot.objects.first().page
        fields = [field for item in page["items"]
                  for action in item["actions"] for field in action["fields"]]
        self.assertTrue(fields)
        for field in fields:
            self.assertTrue(field["renderable"], field["name"])
            self.assertFalse(field["read_only"], field["name"])

    def test_rerunning_the_command_leaves_one_snapshot_per_pipeline(self):
        call_command("bootstrap_demo_access", "--demo", "--skip-admin", "--password", self.password)

        call_command("bootstrap_demo_access", "--demo", "--skip-admin", "--password", self.password)

        for pipeline in AISTPipeline.objects.filter(execution_type=PipelineExecutionType.DAST):
            self.assertEqual(DastQueueSnapshot.objects.filter(pipeline=pipeline).count(), 1)
