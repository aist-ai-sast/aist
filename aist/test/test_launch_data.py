from __future__ import annotations

from django.test import SimpleTestCase

from aist.launch_data import PipelineLaunchData


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
            "project_version_descriptor": {"version": "v1"},
            "imported_test_ids": [10, 20],
        })
        self.assertEqual(ld.log_level, "DEBUG")
        self.assertEqual(ld.languages, ["python", "java"])
        self.assertEqual(ld.ai, {"mode": "AUTO_DEFAULT"})
        self.assertEqual(ld.launch_config_id, "cfg-1")
        self.assertEqual(ld.project_version_descriptor, {"version": "v1"})
        self.assertEqual(ld.imported_test_ids, [10, 20])

    def test_defaults_for_missing_fields(self):
        ld = PipelineLaunchData({})
        self.assertEqual(ld.trim_path, "")
        self.assertEqual(ld.project_path, "")
        self.assertEqual(ld.output_dir, "")
        self.assertIsNone(ld.tmp_analyzer_config_path)
        self.assertIsNone(ld.launch_config_id)
        self.assertEqual(ld.log_level, "INFO")
        self.assertEqual(ld.languages, [])
        self.assertEqual(ld.ai, {})
        self.assertEqual(ld.project_version_descriptor, {})
        self.assertEqual(ld.imported_test_ids, [])

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
        ld.ai = {"mode": "MANUAL"}
        ld.launch_config_id = "cfg-42"
        ld.project_version_descriptor = {"version": "abc"}
        ld.imported_test_ids = [1, 2, 3]

        d = ld.as_dict()
        self.assertEqual(d["log_level"], "DEBUG")
        self.assertEqual(d["languages"], ["go"])
        self.assertEqual(d["ai"], {"mode": "MANUAL"})
        self.assertEqual(d["launch_config_id"], "cfg-42")
        self.assertEqual(d["project_version_descriptor"], {"version": "abc"})
        self.assertEqual(d["imported_test_ids"], [1, 2, 3])

    def test_merge_applies_fields(self):
        ld = PipelineLaunchData({"log_level": "INFO"})
        ld.merge({"log_level": "DEBUG", "project_version_descriptor": {"v": "2"}})
        self.assertEqual(ld.log_level, "DEBUG")
        self.assertEqual(ld.project_version_descriptor, {"v": "2"})

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
