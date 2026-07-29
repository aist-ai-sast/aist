from __future__ import annotations

import json
import logging
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.integrations.dast_report import DastReportExpectations, validate_dast_terminal_result_bytes
from aist.models import (
    AISTPipeline,
    AISTProject,
    AISTProjectVersion,
    AISTStatus,
    DastExecutionState,
    DastProjectBinding,
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


def _validated_report(*, correlation_id: str, findings: list[dict] | None = None, run_id: str = "run-123"):
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
                "dynamic_finding": True,
                "endpoints": ["https://api.example.test/v2/objects/42"],
            },
        ],
        "dast_run_metadata": {
            "run_id": run_id,
            "target": "cloud-app",
            "stand": "qa-1",
            "source_commits": {"backend": ACTUAL_SHA},
        },
    }
    terminal = {
        "contract_version": "2.0",
        "run_id": run_id,
        "status": "succeeded",
        "selection": {"stand_id": "qa-1", "relation": "ancestor", "distance": 2},
        "trigger_resolution": {
            "type": "GIT_BRANCH",
            "ref": TRIGGER_BRANCH,
            "resolved_commit": "b" * 40,
            "resolved_at": "2026-07-26T10:00:00Z",
        },
        "dast_run_metadata": {"source_commits": {"backend": ACTUAL_SHA}},
        "report": report,
        "audit": {"correlation_id": correlation_id, "source_verified": True},
    }
    return validate_dast_terminal_result_bytes(
        json.dumps(terminal).encode(),
        expectations=DastReportExpectations(
            correlation_id=correlation_id,
            run_id=run_id,
            target_id="cloud-app",
            allowed_repository_keys=frozenset({"backend"}),
        ),
    )


class DastFinalizationTests(TestCase):
    def setUp(self):
        dispatch = patch("dojo.importers.default_importer.dojo_dispatch_task")
        dispatch.start()
        self.addCleanup(dispatch.stop)
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
            parameter_schema={"type": "object", "additionalProperties": False},
            provider_defaults={},
            repository_keys=["backend"],
            autonomous_ready=True,
            last_seen_at=timezone.now(),
        )
        self.binding = DastProjectBinding.objects.create(
            project=self.project,
            target=target,
            source_repo_key="backend",
            enabled=True,
        )
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
