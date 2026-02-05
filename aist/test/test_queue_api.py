from __future__ import annotations

from django.urls import reverse

from aist.models import AISTProjectLaunchConfig, PipelineLaunchQueue
from aist.test.test_api import AISTApiBase


class QueueAPITests(AISTApiBase):
    def test_delete_queue_item(self):
        cfg = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Preset",
            description="",
            params={"project_version": {"id": self.pv.id}},
            is_default=False,
        )
        item = PipelineLaunchQueue.objects.create(
            project=self.project,
            launch_config=cfg,
            dispatched=False,
        )
        url = reverse("aist_api:pipeline_launch_queue_detail", kwargs={"queue_id": item.id})
        resp = self.client.delete(url)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(PipelineLaunchQueue.objects.filter(id=item.id).exists())
