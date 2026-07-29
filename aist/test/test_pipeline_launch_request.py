from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from aist.execution.launch_request import LaunchRequestSnapshotError, LaunchRequestSnapshots
from aist.models import (
    AISTApiToken,
    AISTPipeline,
    ApiTokenScope,
    Organization,
    PipelineExecutionType,
    PipelineLaunchAuthorityKind,
    PipelineLaunchOrigin,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)
from aist.test.test_api import AISTApiBase


class LaunchRequestSnapshotTests(TestCase):
    def test_snapshots_are_defensive_and_reject_secret_fields(self):
        params = {"analyzers": ["semgrep"], "nested": {"limit": 5}}
        snapshots = LaunchRequestSnapshots.from_values(params=params, capability={"revision": "v2"})
        params["nested"]["limit"] = 99

        self.assertEqual(snapshots.params_snapshot()["nested"]["limit"], 5)
        with self.assertRaises(LaunchRequestSnapshotError):
            LaunchRequestSnapshots.from_values(
                params={"transport": {"api_token": "must-not-persist"}},
                capability={},
            )


class PipelineLaunchRequestTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.organization = Organization.objects.create(
            name="Launch request organization",
            product_type=self.prod_type,
        )

    def test_request_persists_generic_outbox_fields_without_raw_pat(self):
        token, raw_token = AISTApiToken.issue(
            user=self.user,
            organization=self.organization,
            name="launch-token",
            scope=ApiTokenScope.READ_WRITE,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        request = PipelineLaunchRequest(
            origin=PipelineLaunchOrigin.MANUAL,
            execution_type=PipelineExecutionType.SAST,
            project=self.project,
            requester=self.user,
            api_token=token,
            authority_kind=PipelineLaunchAuthorityKind.PAT,
            params_snapshot={"project_version": {"id": self.pv.id}, "analyzers": ["semgrep"]},
            capability_snapshot={"contract_major": 2, "revision": "catalog-7"},
            state=PipelineLaunchRequestState.PENDING,
            coalesce_key="sast:project:version:params",
            priority=10,
            not_before=timezone.now(),
            expires_at=timezone.now() + timedelta(hours=2),
            failure_code="",
            failure_detail="",
        )
        request.full_clean()
        request.save()

        request.refresh_from_db()
        self.assertEqual(request.api_token_id, token.id)
        self.assertEqual(request.task_id.version, 4)
        self.assertFalse(request.dispatched)
        self.assertNotIn(raw_token, str(request.params_snapshot))
        self.assertNotIn(raw_token, str(request.capability_snapshot))

    def test_snapshot_fields_are_immutable_in_database(self):
        request = PipelineLaunchRequest.objects.create(
            project=self.project,
            params_snapshot={"project_version": {"id": self.pv.id}},
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            PipelineLaunchRequest.objects.filter(pk=request.pk).update(params_snapshot={"changed": True})

    def test_pipeline_relation_is_unique(self):
        pipeline = AISTPipeline.objects.create(
            id="launch-request-pipeline",
            project=self.project,
            project_version=self.pv,
        )
        PipelineLaunchRequest.objects.create(project=self.project, pipeline=pipeline)

        with self.assertRaises(IntegrityError), transaction.atomic():
            PipelineLaunchRequest.objects.create(project=self.project, pipeline=pipeline)

    def test_nested_secret_is_rejected_before_save(self):
        request = PipelineLaunchRequest(
            project=self.project,
            params_snapshot={"connector": {"password": "must-not-persist"}},
        )
        with self.assertRaises(ValidationError):
            request.full_clean()
