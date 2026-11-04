from __future__ import annotations

from django.test import SimpleTestCase

from aist.ai_filter import AIFilterError
from aist.launch_data import AI_FILTER_REQUIRED, AIMode, AITriageType, LaunchAISettings, PipelineLaunchData


class PipelineLaunchDataReadTest(SimpleTestCase):

    """Typed read access over raw launch_data dicts."""

    def test_reads_external_fields(self):
        ld = PipelineLaunchData({
            "trim_path": "/src/",
            "project_path": "/build/project",
            "output_dir": "/tmp/output",  # noqa: S108
            "tmp_analyzer_config_path": "/tmp/cfg.json",  # noqa: S108
        })
        self.assertEqual(ld.trim_path, "/src/")
        self.assertEqual(ld.project_path, "/build/project")
        self.assertEqual(ld.output_dir, "/tmp/output")  # noqa: S108
        self.assertEqual(ld.tmp_analyzer_config_path, "/tmp/cfg.json")  # noqa: S108

    def test_resolved_commit_shortcut(self):
        ld = PipelineLaunchData({"git": {"resolved_commit": "  abc123  "}})
        self.assertEqual(ld.resolved_commit, "abc123")

    def test_resolved_commit_missing(self):
        self.assertEqual(PipelineLaunchData({}).resolved_commit, "")
        self.assertEqual(PipelineLaunchData({"git": {}}).resolved_commit, "")

    def test_reads_internal_fields(self):
        ld = PipelineLaunchData({
            "log_level": "DEBUG",
            "languages": ["python", "java"],
            "ai": {"mode": "AUTO_DEFAULT"},
            "launch_config_id": "cfg-1",
            "imported_test_ids": [10, 20],
            "analyzer_outcomes": [{"name": "agent-security"}],
            "analyzer_versions": [{"analyzer": "semgrep", "tool_version": "semgrep 1.183.0"}],
            "analyzer_degraded_reasons": [{"code": "missing_result"}],
        })
        self.assertEqual(ld.log_level, "DEBUG")
        self.assertEqual(ld.languages, ["python", "java"])
        self.assertEqual(ld.ai, LaunchAISettings(mode=AIMode.AUTO_DEFAULT))
        self.assertEqual(ld.launch_config_id, "cfg-1")
        self.assertEqual(ld.imported_test_ids, [10, 20])
        self.assertEqual(ld.analyzer_outcomes, [{"name": "agent-security"}])
        # sast-combinator owns this shape; AIST stores it verbatim and never rebuilds it.
        self.assertEqual(
            ld.analyzer_versions,
            [{"analyzer": "semgrep", "tool_version": "semgrep 1.183.0"}],
        )
        self.assertTrue(ld.has_analyzer_degraded_reasons)

    def test_defaults_for_missing_fields(self):
        ld = PipelineLaunchData({})
        self.assertEqual(ld.trim_path, "")
        self.assertEqual(ld.project_path, "")
        self.assertEqual(ld.output_dir, "")
        self.assertIsNone(ld.tmp_analyzer_config_path)
        self.assertIsNone(ld.launch_config_id)
        self.assertEqual(ld.log_level, "INFO")
        self.assertEqual(ld.languages, [])
        self.assertEqual(ld.ai, LaunchAISettings())
        self.assertEqual(ld.imported_test_ids, [])
        self.assertEqual(ld.analyzer_outcomes, [])
        self.assertEqual(ld.analyzer_versions, [])
        self.assertEqual(ld.analyzer_degraded_reasons, [])
        self.assertFalse(ld.has_analyzer_degraded_reasons)

    def test_none_data_treated_as_empty(self):
        ld = PipelineLaunchData(None)
        self.assertEqual(ld.log_level, "INFO")
        self.assertEqual(ld.trim_path, "")


class PipelineLaunchDataWriteTest(SimpleTestCase):

    """Setters update the underlying dict and as_dict() round-trips correctly."""

    def test_setters_update_underlying_dict(self):
        ld = PipelineLaunchData({})
        ld.log_level = "DEBUG"
        ld.languages = ["go"]
        ld.ai = LaunchAISettings(mode=AIMode.MANUAL)
        ld.launch_config_id = "cfg-42"
        ld.imported_test_ids = [1, 2, 3]
        ld.add_analyzer_degraded_reasons([{"code": "missing_result"}])

        d = ld.as_dict()
        self.assertEqual(d["log_level"], "DEBUG")
        self.assertEqual(d["languages"], ["go"])
        self.assertEqual(d["ai"], {"mode": "MANUAL", "triage_type": None, "filter_snapshot": None})
        self.assertEqual(d["launch_config_id"], "cfg-42")
        self.assertEqual(d["imported_test_ids"], [1, 2, 3])
        self.assertEqual(d["analyzer_degraded_reasons"], [{"code": "missing_result"}])

    def test_merge_applies_fields(self):
        ld = PipelineLaunchData({"log_level": "INFO"})
        ld.merge({"log_level": "DEBUG", "output_dir": "/srv/output/pipe-1"})
        self.assertEqual(ld.log_level, "DEBUG")
        self.assertEqual(ld.output_dir, "/srv/output/pipe-1")

    def test_unknown_keys_are_preserved(self):
        """Keys from the external sast-combinator package survive round-tripping."""
        raw = {
            "trim_path": "/src/",
            "some_future_external_key": {"nested": True},
            "another_unknown": 42,
        }
        ld = PipelineLaunchData(raw)
        ld.log_level = "DEBUG"
        result = ld.as_dict()

        self.assertEqual(result["some_future_external_key"], {"nested": True})
        self.assertEqual(result["another_unknown"], 42)
        self.assertEqual(result["log_level"], "DEBUG")

    def test_as_dict_returns_same_object(self):
        """as_dict() returns the internal dict directly (no copy overhead)."""
        ld = PipelineLaunchData({"x": 1})
        self.assertIs(ld.as_dict(), ld.as_dict())


class LaunchAISettingsTest(SimpleTestCase):

    """How a launch's findings reach AI triage."""

    def test_findings_go_to_triage_automatically_with_a_filter_or_with_local_triage(self):
        cases = {
            "manual": ({"mode": "MANUAL", "filter_snapshot": {"limit": 5}}, AITriageType.N8N, False),
            "auto with filter": ({"mode": "AUTO_DEFAULT", "filter_snapshot": {"limit": 5}}, AITriageType.N8N, True),
            "auto, n8n, no filter": ({"mode": "AUTO_DEFAULT"}, AITriageType.N8N, False),
            "auto, local, no filter": ({"mode": "AUTO_DEFAULT"}, AITriageType.LOCAL, True),
        }
        for name, (stored, triage_type, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(PipelineLaunchData({"ai": stored}).ai.pushes_automatically(triage_type), expected)

    def test_a_launch_that_would_not_push_for_lack_of_a_filter_is_rejected(self):
        auto = LaunchAISettings.from_params({"ai_mode": "AUTO_DEFAULT"})

        with self.assertRaisesMessage(AIFilterError, AI_FILTER_REQUIRED):
            auto.check(AITriageType.N8N)
        auto.check(AITriageType.LOCAL)
        LaunchAISettings.from_params({"ai_mode": "MANUAL"}).check(AITriageType.N8N)

    def test_launch_params_round_trip_and_manual_drops_the_filter(self):
        severity_high = {"limit": 5, "severity": [{"comparison": "EQUALS", "value": "High"}]}
        auto = LaunchAISettings.from_params(
            {"ai_mode": "AUTO_DEFAULT", "ai_triage_type": "local", "ai_filter_snapshot": severity_high},
        )
        manual = LaunchAISettings.from_params({"ai_mode": "MANUAL", "ai_filter_snapshot": severity_high})

        self.assertEqual(LaunchAISettings.from_params(auto.as_params()), auto)
        self.assertIsNone(manual.filter_snapshot)

    def test_unknown_stored_values_read_as_manual_and_project_default(self):
        ai = PipelineLaunchData({"ai": {"mode": "SOMETIMES", "triage_type": "carrier-pigeon"}}).ai

        self.assertEqual((ai.mode, ai.triage_type), (AIMode.MANUAL, None))

    def test_settings_round_trip_through_launch_data(self):
        ld = PipelineLaunchData({})
        ld.ai = LaunchAISettings(mode=AIMode.AUTO_DEFAULT, triage_type=AITriageType.LOCAL, filter_snapshot={"limit": 5})

        self.assertEqual(PipelineLaunchData(ld.as_dict()).ai, ld.ai)
