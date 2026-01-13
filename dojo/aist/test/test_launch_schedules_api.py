from __future__ import annotations

import json

from django.urls import reverse

from dojo.aist.models import AISTProjectLaunchConfig, LaunchSchedule
from dojo.aist.test.test_api import AISTApiBase


class LaunchSchedulesAPITests(AISTApiBase):
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

    def test_upsert_creates_and_updates(self):
        cfg = self._create_config()
        url = reverse("dojo_aist_api:project_launch_schedule_upsert", kwargs={"project_id": self.project.id})

        resp = self.client.post(
            url,
            data={
                "cron_expression": "*/5 * * * *",
                "enabled": True,
                "max_concurrent_per_worker": 2,
                "launch_config_id": cfg.id,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        data = self._json(resp)
        self.assertTrue(data["created"])

        resp2 = self.client.post(
            url,
            data={
                "cron_expression": "*/10 * * * *",
                "enabled": False,
                "max_concurrent_per_worker": 3,
                "launch_config_id": cfg.id,
            },
            format="json",
        )
        self.assertEqual(resp2.status_code, 201)
        data2 = self._json(resp2)
        self.assertFalse(data2["created"])

        sched = LaunchSchedule.objects.get(launch_config=cfg)
        self.assertEqual(sched.cron_expression, "*/10 * * * *")
        self.assertFalse(sched.enabled)
        self.assertEqual(sched.max_concurrent_per_worker, 3)

    def test_list_and_detail(self):
        cfg = self._create_config()
        sched = LaunchSchedule.objects.create(
            cron_expression="*/5 * * * *",
            enabled=True,
            max_concurrent_per_worker=1,
            launch_config=cfg,
        )

        list_url = reverse("dojo_aist_api:launch_schedule_list")
        resp = self.client.get(list_url, data={"project_id": self.project.id})
        self.assertEqual(resp.status_code, 200)
        results = self._json(resp)
        self.assertTrue(results)

        detail_url = reverse("dojo_aist_api:launch_schedule_detail", kwargs={"launch_schedule_id": sched.id})
        resp2 = self.client.get(detail_url)
        self.assertEqual(resp2.status_code, 200)
        detail = self._json(resp2)
        self.assertEqual(detail["id"], sched.id)

    def test_preview(self):
        url = reverse("dojo_aist_api:launch_schedule_preview")
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
            max_concurrent_per_worker=1,
            launch_config=cfg,
        )
        url = reverse("dojo_aist_api:launch_schedule_bulk_disable")
        resp = self.client.post(url, data={"project_id": self.project.id}, format="json")
        self.assertEqual(resp.status_code, 200)
        LaunchSchedule.objects.get(launch_config=cfg).refresh_from_db()
        self.assertFalse(LaunchSchedule.objects.get(launch_config=cfg).enabled)

    def test_run_once(self):
        cfg = self._create_config()
        sched = LaunchSchedule.objects.create(
            cron_expression="*/5 * * * *",
            enabled=True,
            max_concurrent_per_worker=1,
            launch_config=cfg,
        )
        url = reverse("dojo_aist_api:launch_schedule_run_once", kwargs={"launch_schedule_id": sched.id})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 200)
        data = self._json(resp)
        self.assertTrue(data["ok"])
        self.assertEqual(data["queue_item"]["schedule_id"], sched.id)

    def test_upsert_rejects_invalid_cron(self):
        cfg = self._create_config()
        url = reverse("dojo_aist_api:project_launch_schedule_upsert", kwargs={"project_id": self.project.id})
        resp = self.client.post(
            url,
            data={
                "cron_expression": "not a cron",
                "enabled": True,
                "max_concurrent_per_worker": 1,
                "launch_config_id": cfg.id,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_upsert_rejects_invalid_limit(self):
        cfg = self._create_config()
        url = reverse("dojo_aist_api:project_launch_schedule_upsert", kwargs={"project_id": self.project.id})
        resp = self.client.post(
            url,
            data={
                "cron_expression": "*/5 * * * *",
                "enabled": True,
                "max_concurrent_per_worker": 0,
                "launch_config_id": cfg.id,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_upsert_rejects_missing_launch_config(self):
        url = reverse("dojo_aist_api:project_launch_schedule_upsert", kwargs={"project_id": self.project.id})
        resp = self.client.post(
            url,
            data={
                "cron_expression": "*/5 * * * *",
                "enabled": True,
                "max_concurrent_per_worker": 1,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_list_rejects_invalid_enabled(self):
        url = reverse("dojo_aist_api:launch_schedule_list")
        resp = self.client.get(url, data={"enabled": "maybe"})
        self.assertEqual(resp.status_code, 400)

    def test_list_rejects_invalid_ordering(self):
        url = reverse("dojo_aist_api:launch_schedule_list")
        resp = self.client.get(url, data={"ordering": "bad"})
        self.assertEqual(resp.status_code, 400)

    def test_list_rejects_invalid_pagination(self):
        url = reverse("dojo_aist_api:launch_schedule_list")
        resp = self.client.get(url, data={"limit": "x", "offset": "y"})
        self.assertEqual(resp.status_code, 400)

    def test_preview_rejects_invalid_cron(self):
        url = reverse("dojo_aist_api:launch_schedule_preview")
        resp = self.client.post(url, data={"cron_expression": "bad"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_bulk_disable_requires_scope(self):
        url = reverse("dojo_aist_api:launch_schedule_bulk_disable")
        resp = self.client.post(url, data={}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_run_once_not_found(self):
        url = reverse("dojo_aist_api:launch_schedule_run_once", kwargs={"launch_schedule_id": 999999})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 404)
