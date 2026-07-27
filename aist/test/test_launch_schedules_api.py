from __future__ import annotations

import json

from django.urls import reverse
from rest_framework.test import APIClient

from aist.models import (
    AISTApiToken,
    AISTProjectLaunchConfig,
    ApiTokenScope,
    LaunchSchedule,
    Organization,
    PipelineLaunchRequest,
)
from aist.test.test_api import AISTApiBase


class LaunchSchedulesAPITests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.organization = Organization.objects.create(
            name="Launch schedules API organization",
            product_type=self.prod_type,
        )

    def _json(self, resp):
        return json.loads(resp.content.decode("utf-8") or "{}")

    def _create_config(self):
        return AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Preset",
            description="",
            params={"project_version": {"id": self.pv.id}},
            is_default=True,
        )

    def _schedule_url(self, config):
        return reverse(
            "aist_api:launch_config_schedule",
            kwargs={"project_id": config.project_id, "config_id": config.pk},
        )

    def test_config_scoped_put_supports_two_configs_without_conflict(self):
        first_config = self._create_config()
        second_config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Second preset",
            params={"project_version": {"id": self.pv.id}, "log_level": "DEBUG"},
        )

        first = self.client.put(
            self._schedule_url(first_config),
            data={
                "cron_expression": "*/5 * * * *",
                "enabled": True,
                "max_concurrent_runs": 2,
            },
            format="json",
        )
        second = self.client.put(
            self._schedule_url(second_config),
            data={
                "cron_expression": "15 * * * *",
                "enabled": True,
                "max_concurrent_runs": 1,
            },
            format="json",
        )
        updated = self.client.put(
            self._schedule_url(first_config),
            data={
                "cron_expression": "*/10 * * * *",
                "enabled": False,
                "max_concurrent_runs": 3,
            },
            format="json",
        )
        patched = self.client.patch(
            self._schedule_url(first_config),
            data={"enabled": True},
            format="json",
        )
        retrieved = self.client.get(self._schedule_url(first_config))

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(retrieved.status_code, 200)
        self.assertTrue(retrieved.data["enabled"])
        self.assertEqual(LaunchSchedule.objects.count(), 2)
        sched = LaunchSchedule.objects.get(launch_config=first_config)
        self.assertEqual(sched.cron_expression, "*/10 * * * *")
        self.assertTrue(sched.enabled)
        self.assertEqual(sched.max_concurrent_runs, 3)

    def test_config_scoped_crud_rejects_cross_org_and_read_only_pat_writes(self):
        own_config = self._create_config()
        Organization.objects.create(
            name="Foreign schedule organization",
            product_type=self.other_prod_type,
        )
        foreign_config = AISTProjectLaunchConfig.objects.create(
            project=self.other_project,
            name="Foreign preset",
            params={"project_version": {"id": self.other_pv.id}},
        )
        payload = {
            "cron_expression": "*/5 * * * *",
            "enabled": True,
            "max_concurrent_runs": 1,
        }

        cross_org = self.client.put(self._schedule_url(foreign_config), payload, format="json")
        _token, raw = AISTApiToken.issue(
            user=self.user,
            organization=self.organization,
            name="schedule-read-only",
            scope=ApiTokenScope.READ_ONLY,
        )
        token_client = APIClient()
        token_client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        read_only = token_client.put(self._schedule_url(own_config), payload, format="json")

        self.assertEqual(cross_org.status_code, 404)
        self.assertEqual(read_only.status_code, 403)
        self.assertFalse(LaunchSchedule.objects.exists())

    def test_list_and_detail(self):
        cfg = self._create_config()
        sched = LaunchSchedule.objects.create(
            cron_expression="*/5 * * * *",
            enabled=True,
            max_concurrent_runs=1,
            launch_config=cfg,
        )

        list_url = reverse("aist_api:launch_schedule_list")
        resp = self.client.get(list_url, data={"project_id": self.project.id})
        self.assertEqual(resp.status_code, 200)
        results = self._json(resp)
        self.assertTrue(results)

        detail_url = reverse("aist_api:launch_schedule_detail", kwargs={"launch_schedule_id": sched.id})
        resp2 = self.client.get(detail_url)
        self.assertEqual(resp2.status_code, 200)
        detail = self._json(resp2)
        self.assertEqual(detail["id"], sched.id)

    def test_preview(self):
        url = reverse("aist_api:launch_schedule_preview")
        resp = self.client.post(url, data={"cron_expression": "*/5 * * * *", "count": 3}, format="json")
        self.assertEqual(resp.status_code, 200)
        data = self._json(resp)
        self.assertEqual(data["count"], 3)
        self.assertEqual(len(data["runs"]), 3)

    def test_bulk_disable(self):
        cfg = self._create_config()
        LaunchSchedule.objects.create(
            cron_expression="*/5 * * * *",
            enabled=True,
            max_concurrent_runs=1,
            launch_config=cfg,
        )
        url = reverse("aist_api:launch_schedule_bulk_disable")
        resp = self.client.post(url, data={"project_id": self.project.id}, format="json")
        self.assertEqual(resp.status_code, 200)
        LaunchSchedule.objects.get(launch_config=cfg).refresh_from_db()
        self.assertFalse(LaunchSchedule.objects.get(launch_config=cfg).enabled)

    def test_run_once(self):
        cfg = self._create_config()
        sched = LaunchSchedule.objects.create(
            cron_expression="*/5 * * * *",
            enabled=True,
            max_concurrent_runs=1,
            launch_config=cfg,
        )
        url = reverse("aist_api:launch_schedule_run_once", kwargs={"launch_schedule_id": sched.id})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 200)
        data = self._json(resp)
        self.assertTrue(data["ok"])
        self.assertEqual(data["queue_item"]["schedule_id"], sched.id)

    def test_run_once_idempotency_key_replays_same_queue_item(self):
        cfg = self._create_config()
        sched = LaunchSchedule.objects.create(
            cron_expression="*/5 * * * *",
            enabled=True,
            max_concurrent_runs=1,
            launch_config=cfg,
        )
        url = reverse("aist_api:launch_schedule_run_once", kwargs={"launch_schedule_id": sched.id})

        first = self.client.post(url, HTTP_IDEMPOTENCY_KEY="run-once-42")
        replay = self.client.post(url, HTTP_IDEMPOTENCY_KEY="run-once-42")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(first.data["queue_item"]["id"], replay.data["queue_item"]["id"])
        self.assertEqual(PipelineLaunchRequest.objects.count(), 1)

    def test_delete_schedule(self):
        cfg = self._create_config()
        sched = LaunchSchedule.objects.create(
            cron_expression="*/5 * * * *",
            enabled=True,
            max_concurrent_runs=1,
            launch_config=cfg,
        )
        url = self._schedule_url(cfg)
        resp = self.client.delete(url)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(LaunchSchedule.objects.filter(id=sched.id).exists())

    def test_upsert_rejects_invalid_cron(self):
        cfg = self._create_config()
        resp = self.client.put(
            self._schedule_url(cfg),
            data={
                "cron_expression": "not a cron",
                "enabled": True,
                "max_concurrent_runs": 1,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_upsert_rejects_invalid_limit(self):
        cfg = self._create_config()
        resp = self.client.put(
            self._schedule_url(cfg),
            data={
                "cron_expression": "*/5 * * * *",
                "enabled": True,
                "max_concurrent_runs": 0,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_upsert_rejects_body_launch_config_override(self):
        cfg = self._create_config()
        resp = self.client.put(
            self._schedule_url(cfg),
            data={
                "cron_expression": "*/5 * * * *",
                "enabled": True,
                "max_concurrent_runs": 1,
                "launch_config_id": 999999,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_list_rejects_invalid_enabled(self):
        url = reverse("aist_api:launch_schedule_list")
        resp = self.client.get(url, data={"enabled": "maybe"})
        self.assertEqual(resp.status_code, 400)

    def test_list_rejects_invalid_ordering(self):
        url = reverse("aist_api:launch_schedule_list")
        resp = self.client.get(url, data={"ordering": "bad"})
        self.assertEqual(resp.status_code, 400)

    def test_list_rejects_invalid_pagination(self):
        url = reverse("aist_api:launch_schedule_list")
        resp = self.client.get(url, data={"limit": "x", "offset": "y"})
        self.assertEqual(resp.status_code, 400)

    def test_preview_rejects_invalid_cron(self):
        url = reverse("aist_api:launch_schedule_preview")
        resp = self.client.post(url, data={"cron_expression": "bad"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_bulk_disable_requires_scope(self):
        url = reverse("aist_api:launch_schedule_bulk_disable")
        resp = self.client.post(url, data={}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_run_once_not_found(self):
        url = reverse("aist_api:launch_schedule_run_once", kwargs={"launch_schedule_id": 999999})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 404)
