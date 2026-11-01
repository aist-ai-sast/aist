"""
"Return to step Push To AI" is offered only where the lifecycle permits it.

The lifecycle treats FINISHED and FINISHED_WITH_WARNINGS as terminal: nothing leaves them.
The action was offered on every finished pipeline regardless, and the view let the
resulting ``PipelineTransitionError`` escape, so the button answered with a 500 every time
it was pressed.
"""
from __future__ import annotations

from django.urls import reverse

from aist.models import AISTPipeline, AISTStatus
from aist.test.test_api import AISTApiBase

RETURN_LABEL = "Return to step"


class ReturnToPushToAITests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.client.force_login(self.user)

    def _pipeline(self, pipeline_id: str, status: str) -> AISTPipeline:
        return AISTPipeline.objects.create(
            id=pipeline_id, project=self.project, project_version=self.pv, status=status,
        )

    def _detail(self, pipeline: AISTPipeline) -> str:
        response = self.client.get(
            reverse("aist:pipeline_detail", kwargs={"pipeline_id": pipeline.id}),
        )
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_a_terminal_pipeline_does_not_offer_the_action(self):
        for status in (AISTStatus.FINISHED, AISTStatus.FINISHED_WITH_WARNINGS):
            with self.subTest(status=status):
                pipeline = self._pipeline(f"pipe-terminal-{status}", status)
                self.assertNotIn(RETURN_LABEL, self._detail(pipeline))

    def test_no_status_offers_the_action_today(self):
        """
        The partial holding it renders only for FINISHED and FINISHED_WITH_WARNINGS, and the
        lifecycle lets nothing leave either, so the control has no state where it applies.
        Opening that up is a product decision; this records what ships.
        """
        for status in AISTStatus.values:
            with self.subTest(status=status):
                pipeline = self._pipeline(f"pipe-{status.lower()}", status)
                self.assertNotIn(RETURN_LABEL, self._detail(pipeline))

    def test_a_forbidden_transition_is_refused_without_a_server_error(self):
        """A stale page can still post it, so the view must answer rather than raise."""
        pipeline = self._pipeline("pipe-terminal-post", AISTStatus.FINISHED_WITH_WARNINGS)

        response = self.client.post(
            reverse("aist:pipeline_set_status", kwargs={"pipeline_id": pipeline.id}),
        )

        self.assertEqual(response.status_code, 302)
        pipeline.refresh_from_db()
        self.assertEqual(pipeline.status, AISTStatus.FINISHED_WITH_WARNINGS)

    def test_a_permitted_transition_still_moves_the_pipeline(self):
        pipeline = self._pipeline("pipe-postprocessing-post", AISTStatus.FINDING_POSTPROCESSING)

        response = self.client.post(
            reverse("aist:pipeline_set_status", kwargs={"pipeline_id": pipeline.id}),
        )

        self.assertEqual(response.status_code, 302)
        pipeline.refresh_from_db()
        self.assertEqual(pipeline.status, AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI)
