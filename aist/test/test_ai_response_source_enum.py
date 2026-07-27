"""AISTAIResponse.source identifies where each AI verdict came from."""
from __future__ import annotations

from aist.models import AISTAIResponse, AISTPipeline, AISTStatus
from aist.test.test_api import AISTApiBase


class AISTAIResponseSourceEnumTests(AISTApiBase):

    def setUp(self):
        super().setUp()
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-source-enum",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
        )

    def test_agent_analyzer_value_exists_in_enum(self):
        # Direct value lookup so a typo or rename in models.py is caught.
        self.assertEqual(AISTAIResponse.Source.AGENT_ANALYZER, "agent_analyzer")

    def test_default_source_is_ai_triage(self):
        # Backwards compatibility: every existing creation site (n8n / local
        # triage) creates rows without setting `source` explicitly. Those
        # rows must be classified as the pre-existing post-import triage flow.
        ai_response = AISTAIResponse.objects.create(pipeline=self.pipeline, payload={"results": {}})
        ai_response.refresh_from_db()
        self.assertEqual(ai_response.source, AISTAIResponse.Source.AI_TRIAGE)

    def test_agent_analyzer_source_round_trips(self):
        ai_response = AISTAIResponse.objects.create(
            pipeline=self.pipeline,
            payload={"results": {}},
            source=AISTAIResponse.Source.AGENT_ANALYZER,
        )
        ai_response.refresh_from_db()
        self.assertEqual(ai_response.source, AISTAIResponse.Source.AGENT_ANALYZER)

    def test_filter_by_source_works(self):
        AISTAIResponse.objects.create(pipeline=self.pipeline, payload={"results": {}})
        AISTAIResponse.objects.create(
            pipeline=self.pipeline,
            payload={"results": {}},
            source=AISTAIResponse.Source.AGENT_ANALYZER,
        )
        agent_only = AISTAIResponse.objects.filter(
            pipeline=self.pipeline,
            source=AISTAIResponse.Source.AGENT_ANALYZER,
        )
        self.assertEqual(agent_only.count(), 1)
