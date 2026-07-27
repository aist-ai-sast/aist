from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

from aist.models import (
    AISTPipeline,
    AISTProjectLaunchConfig,
    PipelineExecutionLease,
    PipelineLaunchOrigin,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)
from aist.test.test_api import AISTApiBase


class LaunchRequestAdminAPITests(AISTApiBase):
    def test_manual_and_scheduled_retries_expose_the_same_request_state(self):
        manual = PipelineLaunchRequest.objects.create(
            project=self.project,
            origin=PipelineLaunchOrigin.MANUAL,
            state=PipelineLaunchRequestState.PENDING,
            capacity_retry_count=2,
        )
        scheduled = PipelineLaunchRequest.objects.create(
            project=self.project,
            origin=PipelineLaunchOrigin.SCHEDULE,
            state=PipelineLaunchRequestState.PENDING,
            capacity_retry_count=2,
        )

        response = self.client.get(reverse("aist_api:pipeline_launch_request_list"))

        self.assertEqual(response.status_code, 200)
        by_id = {item["id"]: item for item in response.data["results"]}
        for request in (manual, scheduled):
            item = by_id[request.pk]
            self.assertEqual(item["state"], PipelineLaunchRequestState.PENDING)
            self.assertEqual(item["capacity_retry_count"], 2)
            self.assertEqual(item["not_before"], request.not_before)
            self.assertIn("expires_at", item)

    def test_delete_launch_request(self):
        cfg = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Preset",
            description="",
            params={"project_version": {"id": self.pv.id}},
            is_default=False,
        )
        item = PipelineLaunchRequest.objects.create(
            project=self.project,
            launch_config=cfg,
        )
        url = reverse("aist_api:pipeline_launch_request_detail", kwargs={"request_id": item.id})
        resp = self.client.delete(url)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(PipelineLaunchRequest.objects.filter(id=item.id).exists())

    def _lease(self, request, *, released_at=None):
        now = timezone.now()
        return PipelineExecutionLease.objects.create(
            request=request,
            resource_key=f"sast-project:{request.project_id}",
            slot=0,
            acquired_at=now - timedelta(minutes=5),
            heartbeat_at=now,
            expires_at=now + timedelta(minutes=5),
            released_at=released_at,
        )

    def test_delete_launch_request_with_open_lease_is_conflict_not_a_crash(self):
        """Regression for H12: PROTECT on the lease FK used to turn this into a 500."""
        item = PipelineLaunchRequest.objects.create(
            project=self.project,
            state=PipelineLaunchRequestState.DISPATCHED,
        )
        lease = self._lease(item)

        url = reverse("aist_api:pipeline_launch_request_detail", kwargs={"request_id": item.id})
        resp = self.client.delete(url)

        self.assertEqual(resp.status_code, 409)
        self.assertTrue(PipelineLaunchRequest.objects.filter(id=item.id).exists())
        self.assertTrue(PipelineExecutionLease.objects.filter(id=lease.id, released_at__isnull=True).exists())

    def test_delete_launch_request_mid_flight_is_conflict(self):
        item = PipelineLaunchRequest.objects.create(
            project=self.project,
            state=PipelineLaunchRequestState.PLANNED,
        )
        url = reverse("aist_api:pipeline_launch_request_detail", kwargs={"request_id": item.id})

        resp = self.client.delete(url)

        self.assertEqual(resp.status_code, 409)
        self.assertTrue(PipelineLaunchRequest.objects.filter(id=item.id).exists())

    def test_delete_launch_request_with_released_lease_cascades(self):
        item = PipelineLaunchRequest.objects.create(
            project=self.project,
            state=PipelineLaunchRequestState.DISPATCHED,
        )
        lease = self._lease(item, released_at=timezone.now())
        url = reverse("aist_api:pipeline_launch_request_detail", kwargs={"request_id": item.id})

        resp = self.client.delete(url)

        self.assertEqual(resp.status_code, 204)
        self.assertFalse(PipelineLaunchRequest.objects.filter(id=item.id).exists())
        self.assertFalse(PipelineExecutionLease.objects.filter(id=lease.id).exists())

    def test_clear_dispatched_skips_requests_with_open_lease(self):
        old = timezone.now() - timedelta(days=10)
        stuck = PipelineLaunchRequest.objects.create(
            project=self.project,
            state=PipelineLaunchRequestState.DISPATCHED,
            dispatched_at=old,
        )
        self._lease(stuck)
        finished = PipelineLaunchRequest.objects.create(
            project=self.project,
            state=PipelineLaunchRequestState.DISPATCHED,
            dispatched_at=old,
        )
        self._lease(finished, released_at=timezone.now())

        resp = self.client.post(
            reverse("aist_api:pipeline_launch_request_clear_dispatched"),
            data={"days": 1},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["deleted"], 1)
        self.assertTrue(PipelineLaunchRequest.objects.filter(id=stuck.id).exists())
        self.assertFalse(PipelineLaunchRequest.objects.filter(id=finished.id).exists())


class LaunchRequestCancelAPITests(AISTApiBase):
    def _url(self, request):
        return reverse("aist_api:pipeline_launch_request_cancel", kwargs={"request_id": request.id})

    def _lease(self, request, *, released_at=None):
        now = timezone.now()
        return PipelineExecutionLease.objects.create(
            request=request,
            resource_key=f"sast-project:{request.project_id}",
            slot=0,
            acquired_at=now - timedelta(minutes=5),
            heartbeat_at=now,
            expires_at=now + timedelta(minutes=5),
            released_at=released_at,
        )

    def test_cancel_pending_request(self):
        item = PipelineLaunchRequest.objects.create(
            project=self.project,
            state=PipelineLaunchRequestState.PENDING,
        )

        resp = self.client.post(self._url(item), format="json")

        self.assertEqual(resp.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.state, PipelineLaunchRequestState.CANCELLED)

    def test_cancel_claimed_request_releases_its_lease(self):
        item = PipelineLaunchRequest.objects.create(
            project=self.project,
            state=PipelineLaunchRequestState.CLAIMED,
            claim_owner="worker-1",
        )
        lease = self._lease(item)

        resp = self.client.post(self._url(item), format="json")

        self.assertEqual(resp.status_code, 200)
        item.refresh_from_db()
        lease.refresh_from_db()
        self.assertEqual(item.state, PipelineLaunchRequestState.CANCELLED)
        self.assertIsNotNone(lease.released_at)

    def test_cancel_already_terminal_request_is_conflict(self):
        item = PipelineLaunchRequest.objects.create(
            project=self.project,
            state=PipelineLaunchRequestState.CANCELLED,
        )

        resp = self.client.post(self._url(item), format="json")

        self.assertEqual(resp.status_code, 409)

    @patch("aist.api.queue.stop_pipeline")
    def test_cancel_dispatched_request_stops_the_pipeline_instead(self, mock_stop_pipeline):
        pipeline = AISTPipeline.objects.create(
            id="deadbeefdeadbeefdeadbeefdeadbeef",
            project=self.project,
            project_version=self.pv,
        )
        item = PipelineLaunchRequest.objects.create(
            project=self.project,
            state=PipelineLaunchRequestState.DISPATCHED,
            pipeline=pipeline,
        )

        resp = self.client.post(self._url(item), format="json")

        self.assertEqual(resp.status_code, 200)
        mock_stop_pipeline.assert_called_once_with(pipeline)
        item.refresh_from_db()
        self.assertEqual(item.state, PipelineLaunchRequestState.DISPATCHED)

    def test_cancel_dispatched_request_without_pipeline_is_conflict(self):
        item = PipelineLaunchRequest.objects.create(
            project=self.project,
            state=PipelineLaunchRequestState.DISPATCHED,
        )

        resp = self.client.post(self._url(item), format="json")

        self.assertEqual(resp.status_code, 409)
