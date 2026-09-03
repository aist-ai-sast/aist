from __future__ import annotations

import json
import logging
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from dojo.models import Finding, Product, Product_Type, SLA_Configuration, Test

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
