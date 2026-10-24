from __future__ import annotations

import json
import logging
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from dojo.authorization.roles_permissions import Roles
from dojo.finding.deduplication import dedupe_batch_of_findings
from dojo.models import Finding, Product, Product_Type, Product_Type_Member, Role, SLA_Configuration, Test

from aist.dedupe.custom import _dast_binding_by_finding_id, resolve_dedupe_scope
from aist.integrations.dast_report import validate_dast_report_bytes
from aist.models import (
    AISTPipeline,
    AISTProject,
    AISTProjectVersion,
    AISTStatus,
    DastExecutionState,
    DastProjectBinding,
    DastRunMetadata,
    DastTarget,
    DastTestBinding,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    PipelineExecutionType,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
    RepositoryInfo,
    ScmType,
    VersionType,
)
from aist.parser_overrides import DAST_SCAN_TYPE
from aist.services.dast_finalization import DastFinalizationError, finalize_dast_report
from aist.services.dast_report_routing import resolve_dast_report_routes
from aist.test import dast_fixtures

ACTUAL_SHA = "a" * 40
TRIGGER_BRANCH = "release/2026-07"
LOGGER = logging.getLogger(__name__)


# Real values from run 80c744a2be37d91c07a7a8ef97c520be.
RUN_COVERAGE = {
    "unit": "endpoint",
    "discovered": 784,
    "reachable": 176,
    "analysed": 38,
    "planned": 10,
    "analysed_names": ["analytics3-test-hdw-mx", "cloud-prod-hdw-mx"],
    "beyond_plan_names": ["cloud-prod-hdw-mx"],
}
RUN_TOKEN_USAGE = {
    "total": {"input": 16, "output": 2081, "thinking": 534, "cache_creation": 45877, "cache_read": 799618, "calls": 8},
    "by_phase": {
        "2": {"input": 16, "output": 2081, "thinking": 534, "cache_creation": 45877, "cache_read": 799618, "calls": 8},
    },
    "by_agent_type": {
        "dast-verify": {
            "agents": 5,
            "input": 16,
            "output": 2081,
            "thinking": 534,
            "cache_creation": 45877,
            "cache_read": 799618,
            "calls": 8,
        },
    },
}


def _validated_report(
    *,
    correlation_id: str,
    findings: list[dict] | None = None,
    run_id: str = "run-123",
    run_metadata: dict | None = None,
    target_id: str = "cloud-app",
):
    del correlation_id
    report = {
        "name": "DAST",
        "type": DAST_SCAN_TYPE,
        "version": "backend@aaaaaaaaaaaa",
        "findings": findings if findings is not None else [
            {
                "title": "Cross-tenant object access",
                "severity": "High",
                "description": "A tenant can access another tenant's object.",
                "unique_id_from_tool": "dast-bola-1",
                "vuln_id_from_tool": "dast-bola-1",
                "cwe": 639,
                "vulnerability_ids": ["CVE-2026-12345"],
                "dynamic_finding": True,
                "endpoints": ["https://api.example.test/v2/objects/42"],
                "param": "object_id",
                "service": "https",
                "component_name": "cloud-api",
                "component_version": "2026.8",
            },
        ],
        "dast_run_metadata": {
            "run_id": run_id,
            "target": target_id,
            "stand": "qa-1",
            "source_commits": {"backend": ACTUAL_SHA},
            "delivery_quality": "complete",
            "audit_state": "complete",
            "findings_complete": True,
            **(run_metadata or {}),
        },
    }
    return validate_dast_report_bytes(
        json.dumps(report).encode(),
        target_id=target_id,
        allowed_repository_keys=frozenset({"backend"}),
    )


class DastFinalizationTests(TestCase):
    def setUp(self):
        dispatch = patch("dojo.importers.default_importer.dojo_dispatch_task")
        dispatch.start()
        self.addCleanup(dispatch.stop)
        # Finalization re-dispatches the importer's post-processing once the findings are committed,
        # so the same broker dependency has to be stubbed on this module's own binding.
        finalization_dispatch = patch("aist.services.dast_finalization.dojo_dispatch_task")
        self.post_processing_dispatch = finalization_dispatch.start()
        self.addCleanup(finalization_dispatch.stop)
        product_type = Product_Type.objects.create(name="DAST finalization")
        sla = SLA_Configuration.objects.create(name="DAST finalization SLA")
        self.organization = Organization.objects.create(
            name="DAST finalization organization",
            product_type=product_type,
        )
        product = Product.objects.create(
            name="DAST finalization product",
            description="desc",
            prod_type=product_type,
            sla_configuration=sla,
        )
        repository = RepositoryInfo.objects.create(
            type=ScmType.GITHUB,
            repo_owner="acme",
            repo_name="cloud-platform",
        )
        self.project = AISTProject.objects.create(
            product=product,
            supported_languages=["python"],
            compilable=False,
            profile={},
            repository=repository,
        )
        integration = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.DAST,
            name="DAST finalization integration",
            is_active=True,
        )
        target = DastTarget.objects.create(
            integration=integration,
            provider_id="cloud-app",
            display_name="Cloud app",
            contract_revision="2.0",
            capability_revision="sha256:capability",
            schema_digest="sha256:schema",
            parameter_schema={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
            },
            provider_defaults={},
            repository_keys=["backend"],
            launch_requirements=["repository-trigger"],
            autonomous_ready=True,
            last_seen_at=timezone.now(),
        )
        self.binding = DastProjectBinding.objects.create(
            project=self.project,
            target=target,
            source_repo_key="backend",
            enabled=True,
        )
        self.integration = integration
        self.trigger = AISTProjectVersion.objects.create(
            project=self.project,
            version=TRIGGER_BRANCH,
            version_type=VersionType.GIT_BRANCH,
        )
        self.lead = get_user_model().objects.create_user(username="dast-finalizer")

        self.remote = AISTPipeline.objects.create(
            id="dast-remote-finalize",
            project=self.project,
            trigger_project_version=self.trigger,
            dast_binding=self.binding,
            execution_type=PipelineExecutionType.DAST,
            status=AISTStatus.EXECUTING,
        )
        DastExecutionState.objects.create(pipeline=self.remote, run_id="run-123")
        PipelineLaunchRequest.objects.create(
            project=self.project,
            execution_type=PipelineExecutionType.DAST,
            dast_binding=self.binding,
            trigger_project_version=self.trigger,
            requester=self.lead,
            state=PipelineLaunchRequestState.DISPATCHED,
            pipeline=self.remote,
        )
        self.manual = AISTPipeline.objects.create(
            id="dast-manual-finalize",
            project=self.project,
            dast_binding=self.binding,
            execution_type=PipelineExecutionType.MANUAL_IMPORT,
            status=AISTStatus.ADMITTED,
        )

    def test_remote_and_manual_use_identical_import_and_post_commit_handoff(self):
        report = _validated_report(correlation_id=self.remote.id)

        with self.captureOnCommitCallbacks(execute=False) as remote_callbacks:
            remote_result = finalize_dast_report(
                pipeline_id=self.remote.id,
                report=report,
                binding=self.binding,
                logger=LOGGER,
                lead=self.lead,
            )
        with self.captureOnCommitCallbacks(execute=False) as manual_callbacks:
            manual_result = finalize_dast_report(
                pipeline_id=self.manual.id,
                report=report,
                binding=self.binding,
                logger=LOGGER,
                lead=self.lead,
            )

        self.remote.refresh_from_db()
        self.manual.refresh_from_db()
        self.assertEqual(remote_result.project_version_id, manual_result.project_version_id)
        self.assertEqual(self.remote.project_version_id, self.manual.project_version_id)
        self.assertEqual(self.remote.project_version.version, ACTUAL_SHA)
        self.assertNotEqual(self.remote.project_version_id, self.trigger.pk)
        self.assertEqual(self.remote.status, AISTStatus.UPLOADING_RESULTS)
        self.assertEqual(self.manual.status, AISTStatus.UPLOADING_RESULTS)
        self.assertGreaterEqual(len(remote_callbacks), 1)
        self.assertGreaterEqual(len(manual_callbacks), 1)
        self.assertEqual(self.remote.tests.count(), 1)
        self.assertEqual(self.manual.tests.count(), 1)
        self.assertEqual(self.remote.tests.first().finding_set.count(), 1)
        self.assertEqual(self.manual.tests.first().finding_set.count(), 1)
        self.assertEqual(
            remote_result.finding_ids,
            tuple(self.remote.tests.first().finding_set.values_list("id", flat=True)),
        )
        persisted = self.remote.tests.first().finding_set.get()
        self.assertEqual(persisted.cwe, 639)
        self.assertEqual(persisted.param, "object_id")
        self.assertEqual(persisted.service, "https")
        self.assertEqual(persisted.component_name, "cloud-api")
        self.assertEqual(persisted.component_version, "2026.8")
        self.assertEqual(
            set(persisted.vulnerability_ids),
            {"CVE-2026-12345"},
        )
        self.assertEqual(
            set(persisted.endpoints.values_list("host", "path")),
            {("api.example.test", "v2/objects/42")},
        )

    def test_finalization_refuses_a_different_binding_before_importing_any_result(self):
        other_target = DastTarget.objects.create(
            integration=self.integration,
            provider_id="other-cloud-app",
            display_name="Other cloud app",
            contract_revision="2.0",
            capability_revision="sha256:other-capability",
            schema_digest="sha256:other-schema",
            parameter_schema={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
            },
            provider_defaults={},
            repository_keys=["backend"],
            launch_requirements=["repository-trigger"],
            autonomous_ready=True,
            last_seen_at=timezone.now(),
        )
        other_binding = DastProjectBinding.objects.create(
            project=self.project,
            target=other_target,
            source_repo_key="backend",
            enabled=True,
        )
        report = _validated_report(
            correlation_id=self.remote.id,
            target_id=other_target.provider_id,
        )
        before = (Test.objects.count(), Finding.objects.count(), DastRunMetadata.objects.count())

        with self.assertRaisesRegex(DastFinalizationError, "binding does not match"):
            finalize_dast_report(
                pipeline_id=self.remote.id,
                report=report,
                binding=other_binding,
                logger=LOGGER,
                lead=self.lead,
            )

        self.assertEqual(
            (Test.objects.count(), Finding.objects.count(), DastRunMetadata.objects.count()),
            before,
        )
        self.assertFalse(self.remote.tests.exists())

    def test_autonomous_finalization_refuses_a_report_from_another_provider_run(self):
        report = _validated_report(correlation_id=self.remote.id, run_id="stale-run")
        before = (Test.objects.count(), Finding.objects.count(), DastRunMetadata.objects.count())

        with self.assertRaisesRegex(DastFinalizationError, "provider run recorded"):
            finalize_dast_report(
                pipeline_id=self.remote.id,
                report=report,
                binding=self.binding,
                logger=LOGGER,
                lead=self.lead,
            )

        self.assertEqual(
            (Test.objects.count(), Finding.objects.count(), DastRunMetadata.objects.count()),
            before,
        )
        self.assertFalse(self.remote.tests.exists())

    def test_duplicate_finalize_returns_persisted_result_without_second_import(self):
        report = _validated_report(correlation_id=self.remote.id)
        with self.captureOnCommitCallbacks(execute=False):
            first = finalize_dast_report(
                pipeline_id=self.remote.id,
                report=report,
                binding=self.binding,
                logger=LOGGER,
                lead=self.lead,
            )
        test_count = self.remote.tests.count()

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            second = finalize_dast_report(
                pipeline_id=self.remote.id,
                report=report,
                binding=self.binding,
                logger=LOGGER,
                lead=self.lead,
            )

        self.assertFalse(first.already_finalized)
        self.assertTrue(second.already_finalized)
        self.assertEqual(first.test_id, second.test_id)
        self.assertEqual(self.remote.tests.count(), test_count)
        self.assertEqual(callbacks, [])

    def test_clean_report_finishes_only_from_post_commit_callback(self):
        report = _validated_report(correlation_id=self.remote.id, findings=[])

        with patch("aist.services.pipeline_results.finish_pipeline") as finish_pipeline:
            with self.captureOnCommitCallbacks(execute=True):
                result = finalize_dast_report(
                    pipeline_id=self.remote.id,
                    report=report,
                    binding=self.binding,
                    logger=LOGGER,
                    lead=self.lead,
                )
            finish_pipeline.assert_called_once_with(self.remote.id)

        self.assertEqual(result.finding_ids, ())

    def test_conflicting_second_report_is_rejected_without_new_test(self):
        first = _validated_report(correlation_id=self.remote.id)
        second = _validated_report(
            correlation_id=self.remote.id,
            findings=[{
                "title": "A different finding",
                "severity": "Low",
                "description": "Different validated report content.",
            }],
        )
        with self.captureOnCommitCallbacks(execute=False):
            finalize_dast_report(
                pipeline_id=self.remote.id,
                report=first,
                binding=self.binding,
                logger=LOGGER,
                lead=self.lead,
            )
        test_count = self.remote.tests.count()

        with self.assertRaisesRegex(DastFinalizationError, "different DAST report"):
            finalize_dast_report(
                pipeline_id=self.remote.id,
                report=second,
                binding=self.binding,
                logger=LOGGER,
                lead=self.lead,
            )

        self.assertEqual(self.remote.tests.count(), test_count)

    def test_run_metadata_lands_on_the_pipeline_for_an_autonomous_run(self):
        report = _validated_report(
            correlation_id=self.remote.id,
            run_metadata={"coverage": RUN_COVERAGE, "token_usage": RUN_TOKEN_USAGE, "tier": "external"},
        )

        with self.captureOnCommitCallbacks(execute=False):
            finalize_dast_report(
                pipeline_id=self.remote.id,
                report=report,
                binding=self.binding,
                logger=LOGGER,
                lead=self.lead,
            )

        row = DastRunMetadata.objects.get(pipeline_id=self.remote.id)
        self.assertEqual(row.run_id, "run-123")
        self.assertEqual(row.stand_id, "qa-1")
        self.assertEqual(row.tier, "external")
        self.assertEqual((row.discovered, row.reachable, row.analysed, row.planned), (784, 176, 38, 10))
        self.assertEqual(row.beyond_plan_names, ["cloud-prod-hdw-mx"])
        self.assertEqual(row.output_tokens, 2081)
        self.assertEqual(row.model_calls, 8)
        self.assertTrue(row.token_accounting_consistent)

    def test_run_metadata_lands_the_same_way_for_an_operator_upload(self):
        report = _validated_report(
            correlation_id=self.manual.id,
            run_metadata={"coverage": RUN_COVERAGE, "token_usage": RUN_TOKEN_USAGE},
        )

        with self.captureOnCommitCallbacks(execute=False):
            finalize_dast_report(
                pipeline_id=self.manual.id,
                report=report,
                binding=self.binding,
                logger=LOGGER,
                lead=self.lead,
            )

        row = DastRunMetadata.objects.get(pipeline_id=self.manual.id)
        self.assertEqual(row.analysed, 38)
        self.assertEqual(row.model_calls, 8)

    def test_a_report_without_the_new_blocks_still_finalizes_and_stores_no_counters(self):
        report = _validated_report(correlation_id=self.remote.id)

        with self.captureOnCommitCallbacks(execute=False):
            finalize_dast_report(
                pipeline_id=self.remote.id,
                report=report,
                binding=self.binding,
                logger=LOGGER,
                lead=self.lead,
            )

        row = DastRunMetadata.objects.get(pipeline_id=self.remote.id)
        # Absent must stay absent: a run that reported nothing must never read as zero.
        self.assertIsNone(row.analysed)
        self.assertIsNone(row.analysed_names)
        self.assertIsNone(row.output_tokens)
        self.assertIsNone(row.token_by_phase)
        self.assertIsNone(row.token_accounting_consistent)

    def test_redelivering_the_same_report_keeps_exactly_one_metadata_row(self):
        report = _validated_report(
            correlation_id=self.remote.id,
            run_metadata={"coverage": RUN_COVERAGE},
        )

        for _ in range(2):
            with self.captureOnCommitCallbacks(execute=False):
                finalize_dast_report(
                    pipeline_id=self.remote.id,
                    report=report,
                    binding=self.binding,
                    logger=LOGGER,
                    lead=self.lead,
                )

        self.assertEqual(DastRunMetadata.objects.filter(pipeline_id=self.remote.id).count(), 1)
        self.assertEqual(DastRunMetadata.objects.get(pipeline_id=self.remote.id).analysed, 38)

    def test_a_pipeline_finalized_before_this_table_existed_gains_its_row_on_redelivery(self):
        report = _validated_report(
            correlation_id=self.remote.id,
            run_metadata={"coverage": RUN_COVERAGE},
        )
        with self.captureOnCommitCallbacks(execute=False):
            finalize_dast_report(
                pipeline_id=self.remote.id,
                report=report,
                binding=self.binding,
                logger=LOGGER,
                lead=self.lead,
            )
        # Stand in for a pipeline whose report was accepted before the metadata was persisted:
        # the finalization marker is already there, the row is not.
        DastRunMetadata.objects.filter(pipeline_id=self.remote.id).delete()

        with self.captureOnCommitCallbacks(execute=False):
            result = finalize_dast_report(
                pipeline_id=self.remote.id,
                report=report,
                binding=self.binding,
                logger=LOGGER,
                lead=self.lead,
            )

        self.assertTrue(result.already_finalized)
        self.assertEqual(DastRunMetadata.objects.get(pipeline_id=self.remote.id).analysed, 38)

    def test_post_processing_is_dispatched_only_after_the_findings_are_committed(self):
        """
        The defect this guards: the importer queues post-processing with `apply_async` from inside
        finalization's still-open transaction, so the worker reads finding ids that are not visible
        yet, finds an empty batch and returns. Deduplication then never runs, no ProcessedFinding
        row is written, and the pipeline sits in WAITING_DEDUPLICATION_TO_FINISH until the
        hour-long dedup deadline force-releases it.
        """
        report = _validated_report(correlation_id=self.remote.id)

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            result = finalize_dast_report(
                pipeline_id=self.remote.id,
                report=report,
                binding=self.binding,
                logger=LOGGER,
                lead=self.lead,
            )
        # Nothing may be queued while the transaction is still open.
        self.assertEqual(self.post_processing_dispatch.call_count, 0)

        for callback in callbacks:
            callback()

        self.assertEqual(self.post_processing_dispatch.call_count, 1)
        _dispatch_args, dispatch_kwargs = self.post_processing_dispatch.call_args
        dispatched_ids = self.post_processing_dispatch.call_args[0][1]
        self.assertEqual(sorted(dispatched_ids), sorted(result.finding_ids))
        self.assertTrue(dispatch_kwargs["dedupe_option"])

    def _sectioned_report(self):
        self.binding.target.repository_keys = ["backend", "portal"]
        self.binding.target.save(update_fields=["repository_keys"])
        role, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=self.organization.product_type, user=self.lead, role=role)
        product = Product.objects.create(
            name="Portal", description="Portal", prod_type=self.project.product.prod_type,
            sla_configuration=self.project.product.sla_configuration,
        )
        repository = RepositoryInfo.objects.create(type=ScmType.GITHUB, repo_owner="acme", repo_name="portal")
        project = AISTProject.objects.create(product=product, repository=repository)
        destination = DastProjectBinding.objects.create(
            project=project, target=self.binding.target, source_repo_key="portal", enabled=True,
        )
        payload = json.loads(_validated_report(correlation_id=self.remote.id).canonical_json)
        findings = payload.pop("findings")
        payload["results"] = [
            {"repository": "backend", "version": "backend@a", "findings": findings},
            {"repository": "portal", "version": "portal@b", "findings": findings},
        ]
        payload["dast_run_metadata"]["source_commits"]["portal"] = "b" * 40
        report = validate_dast_report_bytes(
            json.dumps(payload).encode(), target_id=self.binding.target.provider_id,
            allowed_repository_keys=frozenset({"backend", "portal"}),
        )
        return destination, report

    def test_sections_land_in_their_products_and_redelivery_is_atomic(self):
        destination, report = self._sectioned_report()
        with self.captureOnCommitCallbacks(execute=False):
            first = finalize_dast_report(
                pipeline_id=self.remote.id, report=report, binding=self.binding, logger=LOGGER, lead=self.lead,
            )
        self.assertEqual(len(first.sections), 2)
        self.remote.refresh_from_db()
        self.assertEqual(self.remote.project_id, self.project.pk)
        self.assertEqual(self.remote.tests.count(), 2)
        portal = first.sections[1]
        version = AISTProjectVersion.objects.get(pk=portal.project_version_id)
        self.assertEqual((version.project_id, version.version), (destination.project_id, "b" * 40))
        self.assertEqual(set(version.findings.values_list("pk", flat=True)), set(portal.finding_ids))
        self.assertEqual(Test.objects.get(pk=portal.test_id).engagement.product_id, destination.project.product_id)
        destination.enabled = False
        destination.save(update_fields=["enabled"])
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            second = finalize_dast_report(
                pipeline_id=self.remote.id, report=report, binding=self.binding, logger=LOGGER, lead=self.lead,
            )
        self.assertTrue(second.already_finalized)
        self.assertEqual(second.sections, first.sections)
        self.assertEqual(callbacks, [])
        self.assertEqual(self.remote.tests.count(), 2)

    def test_disabled_destination_keeps_all_findings_on_launch_project(self):
        destination, report = self._sectioned_report()
        destination.enabled = False
        destination.save(update_fields=["enabled"])
        with self.captureOnCommitCallbacks(execute=False):
            result = finalize_dast_report(
                pipeline_id=self.remote.id, report=report, binding=self.binding, logger=LOGGER, lead=self.lead,
            )
        self.assertEqual(len(result.finding_ids), 2)
        self.assertEqual({s.project_version_id for s in result.sections}, {result.project_version_id})
        self.assertEqual({s.binding_id for s in result.sections}, {self.binding.pk})

    def test_second_section_import_failure_rolls_back_first_section(self):
        _destination, report = self._sectioned_report()
        payload = json.loads(report.canonical_json)
        payload["results"][1]["findings"][0].pop("description")
        report = validate_dast_report_bytes(
            json.dumps(payload).encode(), target_id=report.target_id,
            allowed_repository_keys=frozenset({"backend", "portal"}),
        )
        before = (Test.objects.count(), Finding.objects.count())
        with self.captureOnCommitCallbacks(execute=False) as callbacks, self.assertRaises(ValidationError):
            finalize_dast_report(
                pipeline_id=self.remote.id, report=report, binding=self.binding, logger=LOGGER, lead=self.lead,
            )
        self.assertEqual((Test.objects.count(), Finding.objects.count()), before)
        self.assertEqual(callbacks, [])
        self.remote.refresh_from_db()
        self.assertNotIn("dast_finalization", self.remote.launch_data or {})

    def test_missing_commit_and_ambiguous_binding_fall_back_without_losing_sections(self):
        destination, report = self._sectioned_report()
        payload = json.loads(report.canonical_json)
        del payload["dast_run_metadata"]["source_commits"]["portal"]
        no_commit = validate_dast_report_bytes(
            json.dumps(payload).encode(), target_id=report.target_id,
            allowed_repository_keys=frozenset({"backend", "portal"}),
        )
        route = resolve_dast_report_routes(no_commit, self.binding)[1]
        self.assertEqual((route.binding.pk, route.fallback_reason), (self.binding.pk, "missing_source_commit"))
        destination.source_repo_key = "backend"
        destination.save(update_fields=["source_repo_key"])
        route = resolve_dast_report_routes(report, self.binding)[0]
        self.assertEqual((route.binding.pk, route.fallback_reason), (self.binding.pk, "ambiguous_binding"))

    def test_routing_does_not_use_another_targets_binding(self):
        destination, report = self._sectioned_report()
        target = self.binding.target
        target.pk = None
        target.provider_id = "another-target"
        target.save()
        destination.target = target
        destination.save(update_fields=["target"])
        route = resolve_dast_report_routes(report, self.binding)[1]
        self.assertEqual((route.binding.pk, route.fallback_reason), (self.binding.pk, "no_enabled_binding"))

    def test_legacy_finalization_marker_still_short_circuits(self):
        report = _validated_report(correlation_id=self.remote.id)
        with self.captureOnCommitCallbacks(execute=False):
            result = finalize_dast_report(
                pipeline_id=self.remote.id, report=report, binding=self.binding, logger=LOGGER, lead=self.lead,
            )
        self.remote.refresh_from_db()
        marker = self.remote.launch_data["dast_finalization"]
        marker.update(
            version="1", project_version_id=result.project_version_id,
            test_id=result.test_id, finding_ids=list(result.finding_ids),
        )
        self.remote.save(update_fields=["launch_data"])
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            repeated = finalize_dast_report(
                pipeline_id=self.remote.id, report=report, binding=self.binding, logger=LOGGER, lead=self.lead,
            )
        self.assertTrue(repeated.already_finalized)
        self.assertEqual(repeated.finding_ids, result.finding_ids)
        self.assertEqual(callbacks, [])

    @override_settings(
        FINDING_DEDUPE_METHOD="aist.dedupe.custom.custom_dedupe_finding",
        FINDING_DEDUPE_BATCH_METHOD="aist.dedupe.custom.custom_dedupe_batch",
    )
    def test_routed_findings_share_namespace_with_destination_history(self):
        destination, report = self._sectioned_report()
        history = AISTPipeline.objects.create(
            project=destination.project, dast_binding=destination,
            execution_type=PipelineExecutionType.MANUAL_IMPORT, status=AISTStatus.ADMITTED,
        )
        with self.captureOnCommitCallbacks(execute=False):
            old = finalize_dast_report(
                pipeline_id=history.pk, report=report, binding=destination, logger=LOGGER, lead=self.lead,
            )
            current = finalize_dast_report(
                pipeline_id=self.remote.pk, report=report, binding=self.binding, logger=LOGGER, lead=self.lead,
            )
        current_finding = Finding.objects.select_related("test__engagement", "test__test_type").get(
            pk=current.sections[1].finding_ids[0],
        )
        old_id = old.sections[1].finding_ids[0]
        # Historical tests have no explicit destination and inherit their launch binding.
        DastTestBinding.objects.filter(test_id=old.sections[1].test_id).delete()
        self.assertEqual(_dast_binding_by_finding_id([current_finding]), {current_finding.pk: destination.pk})
        self.assertIn(old_id, {finding.pk for finding in resolve_dedupe_scope([current_finding])})
        self.assertNotIn(current.sections[0].finding_ids[0], {
            finding.pk for finding in resolve_dedupe_scope([current_finding])
        })
        dedupe_batch_of_findings([current_finding])
        current_finding.refresh_from_db()
        self.assertTrue(current_finding.duplicate)
        self.assertEqual(current_finding.duplicate_finding_id, old_id)
        DastTestBinding.objects.filter(test_id=current_finding.test_id).update(binding=None)
        self.assertIsNone(_dast_binding_by_finding_id([current_finding])[current_finding.pk])

    def test_cross_project_import_requires_destination_authority(self):
        _destination, report = self._sectioned_report()
        outsider = get_user_model().objects.create_user(username="no-destination-access")
        before = Test.objects.count()
        with self.assertRaises(PermissionError):
            finalize_dast_report(
                pipeline_id=self.remote.pk, report=report, binding=self.binding, logger=LOGGER, lead=outsider,
            )
        self.assertEqual(Test.objects.count(), before)

    def test_invalid_completion_receipt_never_causes_a_second_import(self):
        report = _validated_report(correlation_id=self.remote.id)
        with self.captureOnCommitCallbacks(execute=False):
            finalize_dast_report(
                pipeline_id=self.remote.pk, report=report, binding=self.binding, logger=LOGGER, lead=self.lead,
            )
        self.remote.refresh_from_db()
        receipt = self.remote.launch_data["dast_finalization"]
        self.assertEqual(set(receipt), {"binding_id", "run_id", "correlation_id", "report_sha256"})
        for invalid in ({**receipt, "binding_id": True}, {**receipt, "sections": []}, {"run_id": report.run_id}):
            with self.subTest(receipt=invalid):
                self.remote.launch_data["dast_finalization"] = invalid
                self.remote.save(update_fields=["launch_data"])
                with self.assertRaisesRegex(DastFinalizationError, "receipt is invalid"):
                    finalize_dast_report(
                        pipeline_id=self.remote.pk, report=report, binding=self.binding, logger=LOGGER, lead=self.lead,
                    )
                self.assertEqual(self.remote.tests.count(), 1)

    def test_revoked_destination_access_before_finalization_leaves_no_partial_results(self):
        destination, report = self._sectioned_report()
        Product_Type_Member.objects.filter(user=self.lead).delete()
        before = Test.objects.count()
        with self.assertRaises(PermissionError):
            finalize_dast_report(
                pipeline_id=self.remote.pk, report=report, binding=self.binding, logger=LOGGER, lead=self.lead,
            )
        self.assertEqual(Test.objects.count(), before)
        self.assertFalse(AISTProjectVersion.objects.filter(project_id=destination.project_id).exists())

    def test_same_provider_and_repository_keys_in_another_tenant_never_receive_a_section(self):
        destination, report = self._sectioned_report()
        destination.enabled = False
        destination.save(update_fields=["enabled"])
        product_type = Product_Type.objects.create(name="Other tenant")
        organization = Organization.objects.create(name="Other tenant", product_type=product_type)
        integration, _state = dast_fixtures.create_dast_integration(organization=organization)
        target = dast_fixtures.create_dast_target(
            integration=integration,
            wire=dast_fixtures.target_wire(provider_id=report.target_id, repository_keys=["portal"]),
        )
        product = Product.objects.create(
            name="Other tenant portal", description="Portal", prod_type=product_type,
            sla_configuration=self.project.product.sla_configuration,
        )
        project = AISTProject.objects.create(
            product=product,
            repository=RepositoryInfo.objects.create(type=ScmType.GITHUB, repo_owner="other", repo_name="portal"),
        )
        dast_fixtures.create_dast_binding(project=project, target=target)
        route = resolve_dast_report_routes(report, self.binding)[1]
        self.assertEqual((route.binding.pk, route.fallback_reason), (self.binding.pk, "no_enabled_binding"))
