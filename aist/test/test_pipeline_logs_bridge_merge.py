"""
Tests for the merged celeryworker + aist-triage-bridge log API surface.

Background: claude-bridge runs as the unprivileged ``claude`` user; on Linux
it cannot append to a ``<pipeline_id>.log`` file owned by ``root`` (which
is the case when celeryworker creates it first). The bridge therefore
writes to its own ``<pipeline_id>.bridge.log`` and the API merges both
on read. These tests pin both sides of the contract: the merge logic
in ``pipeline_logs_full_response`` / ``pipeline_logs_progressive_response``
and the path resolution in ``get_pipeline_bridge_log_path``.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.urls import reverse

from aist.logging_transport import (
    _aist_log_dir,
    get_pipeline_bridge_log_path,
    get_pipeline_log_path,
)
from aist.models import AISTPipeline, AISTStatus
from aist.test.test_api import AISTApiBase


class AistLogDirResolutionTests(AISTApiBase):

    """Verify env var > Django setting > MEDIA_ROOT/aist_logs precedence."""

    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="aist-log-dir-tests-")
        self._base_dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()
        super().tearDown()

    def test_env_variable_wins_over_settings(self):
        env_dir = self._base_dir / "from-env"
        settings_dir = self._base_dir / "from-settings"
        with patch.dict(os.environ, {"AIST_LOG_DIR": str(env_dir)}, clear=False), \
             self.settings(AIST_LOG_DIR=str(settings_dir)):
            self.assertEqual(str(_aist_log_dir()), str(env_dir))

    def test_settings_wins_over_media_root(self):
        env_without = {k: v for k, v in os.environ.items() if k != "AIST_LOG_DIR"}
        settings_dir = self._base_dir / "from-settings"
        with patch.dict(os.environ, env_without, clear=True), \
             self.settings(AIST_LOG_DIR=str(settings_dir)):
            self.assertEqual(str(_aist_log_dir()), str(settings_dir))

    def test_falls_back_to_media_root(self):
        env_without = {k: v for k, v in os.environ.items() if k != "AIST_LOG_DIR"}
        media_root = self._base_dir / "media-test-root"
        with patch.dict(os.environ, env_without, clear=True), \
             self.settings(AIST_LOG_DIR="", MEDIA_ROOT=str(media_root)):
            self.assertEqual(str(_aist_log_dir()), str(media_root / "aist_logs"))

    def test_bridge_path_uses_same_dir_as_main_path(self):
        # Bridge and celeryworker MUST write to the same shared volume —
        # otherwise the merged read sees only one side.
        main = get_pipeline_log_path("pipe-bridge-paths")
        bridge = get_pipeline_bridge_log_path("pipe-bridge-paths")
        self.assertEqual(main.parent, bridge.parent)
        self.assertEqual(main.name, "pipe-bridge-paths.log")
        self.assertEqual(bridge.name, "pipe-bridge-paths.bridge.log")


class PipelineLogsFullMergeTests(AISTApiBase):

    """`pipeline_logs_full_response` merges both files chronologically."""

    def setUp(self):
        super().setUp()
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-merge-full",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
        )
        self.main_path = get_pipeline_log_path(self.pipeline.id)
        self.bridge_path = get_pipeline_bridge_log_path(self.pipeline.id)
        self.main_path.parent.mkdir(parents=True, exist_ok=True)

    def _write_both(self, main: str, bridge: str) -> None:
        self.main_path.write_text(main, encoding="utf-8")
        self.bridge_path.write_text(bridge, encoding="utf-8")

    def _get(self) -> str:
        resp = self.client.get(
            reverse("aist_api:pipeline_logs_full", kwargs={"pipeline_id": self.pipeline.id}),
        )
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode("utf-8")

    def test_merges_in_timestamp_order(self):
        self._write_both(
            main=(
                "2026-04-29 10:00:00,000 [INFO] pipeline started\n"
                "2026-04-29 10:00:05,000 [INFO] pipeline finished\n"
            ),
            bridge=(
                "2026-04-29 10:00:02,000 [INFO] bridge invocation begin\n"
                "2026-04-29 10:00:03,000 [INFO] claude exited\n"
            ),
        )
        body = self._get()
        # Order MUST be: pipeline started → bridge begin → claude exited → pipeline finished
        idx_start = body.index("pipeline started")
        idx_bridge_begin = body.index("bridge invocation begin")
        idx_bridge_end = body.index("claude exited")
        idx_pipe_end = body.index("pipeline finished")
        self.assertLess(idx_start, idx_bridge_begin)
        self.assertLess(idx_bridge_begin, idx_bridge_end)
        self.assertLess(idx_bridge_end, idx_pipe_end)

    def test_bridge_lines_are_prefixed(self):
        self._write_both(
            main="2026-04-29 10:00:00,000 [INFO] hello\n",
            bridge="2026-04-29 10:00:01,000 [INFO] from-bridge\n",
        )
        body = self._get()
        self.assertIn("[bridge] 2026-04-29 10:00:01,000 [INFO] from-bridge", body)
        # Main line is NOT prefixed.
        self.assertIn("\n2026-04-29 10:00:00,000 [INFO] hello", "\n" + body)
        self.assertNotIn("[bridge] 2026-04-29 10:00:00,000 [INFO] hello", body)

    def test_main_only_returns_main_content(self):
        self._write_both(main="2026-04-29 10:00:00,000 [INFO] x\n", bridge="")
        body = self._get()
        self.assertIn("[INFO] x", body)
        self.assertNotIn("[bridge]", body)

    def test_bridge_only_returns_prefixed_content(self):
        self._write_both(main="", bridge="2026-04-29 10:00:00,000 [INFO] y\n")
        body = self._get()
        self.assertIn("[bridge] 2026-04-29 10:00:00,000 [INFO] y", body)

    def test_continuation_lines_stay_with_their_record(self):
        # Multi-line bridge message — the indented traceback lines must remain
        # attached to their header in the merged output, not float to the top.
        self._write_both(
            main="2026-04-29 10:00:00,000 [INFO] before\n2026-04-29 10:00:10,000 [INFO] after\n",
            bridge=(
                "2026-04-29 10:00:05,000 [ERROR] bridge failed\n"
                "  Traceback (most recent call last):\n"
                "    File 'a.py', line 1\n"
                "  RuntimeError: boom\n"
            ),
        )
        body = self._get()
        bridge_block_start = body.index("[bridge] 2026-04-29 10:00:05,000")
        traceback_pos = body.index("Traceback (most recent call last)")
        runtime_err_pos = body.index("RuntimeError: boom")
        after_pos = body.index("[INFO] after")
        self.assertLess(bridge_block_start, traceback_pos)
        self.assertLess(traceback_pos, runtime_err_pos)
        self.assertLess(runtime_err_pos, after_pos, "continuation lines must precede later main lines")


class PipelineLogsProgressiveMergeTests(AISTApiBase):

    """Progressive endpoint serves merged delta + per-source size headers."""

    def setUp(self):
        super().setUp()
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-merge-prog",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
        )
        self.main_path = get_pipeline_log_path(self.pipeline.id)
        self.bridge_path = get_pipeline_bridge_log_path(self.pipeline.id)
        self.main_path.parent.mkdir(parents=True, exist_ok=True)
        self.url = reverse(
            "aist_api:pipeline_logs_progressive",
            kwargs={"pipeline_id": self.pipeline.id},
        )

    def _write(self, *, main: str = "", bridge: str = "") -> None:
        self.main_path.write_text(main, encoding="utf-8")
        self.bridge_path.write_text(bridge, encoding="utf-8")

    def test_returns_two_size_headers(self):
        self._write(
            main="2026-04-29 10:00:00,000 [INFO] m\n",
            bridge="2026-04-29 10:00:01,000 [INFO] b\n",
        )
        resp = self.client.get(self.url, data={"tail": 100})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(int(resp["X-Log-Size"]), self.main_path.stat().st_size)
        self.assertEqual(int(resp["X-Bridge-Log-Size"]), self.bridge_path.stat().st_size)

    def test_initial_tail_merges_both_sources(self):
        self._write(
            main="2026-04-29 10:00:00,000 [INFO] m1\n",
            bridge="2026-04-29 10:00:00,500 [INFO] b1\n",
        )
        resp = self.client.get(self.url, data={"tail": 100})
        body = resp.content.decode("utf-8")
        self.assertIn("[INFO] m1", body)
        self.assertIn("[bridge] 2026-04-29 10:00:00,500 [INFO] b1", body)

    def test_delta_uses_independent_offsets(self):
        # Step 1: client receives initial tail.
        self._write(
            main="2026-04-29 10:00:00,000 [INFO] m1\n",
            bridge="2026-04-29 10:00:01,000 [INFO] b1\n",
        )
        first = self.client.get(self.url, data={"tail": 100})
        main_off = int(first["X-Log-Size"])
        bridge_off = int(first["X-Bridge-Log-Size"])

        # Step 2: both sides append. Delta poll must return only new content.
        with self.main_path.open("a", encoding="utf-8") as f:
            f.write("2026-04-29 10:00:02,000 [INFO] m2\n")
        with self.bridge_path.open("a", encoding="utf-8") as f:
            f.write("2026-04-29 10:00:03,000 [INFO] b2\n")

        delta = self.client.get(self.url, data={"start": main_off, "bridge_start": bridge_off})
        body = delta.content.decode("utf-8")
        self.assertIn("[INFO] m2", body)
        self.assertIn("[bridge] 2026-04-29 10:00:03,000 [INFO] b2", body)
        # Old lines MUST NOT reappear in the delta — that's the duplication
        # guarantee the per-source offsets buy us.
        self.assertNotIn("[INFO] m1", body)
        self.assertNotIn("[INFO] b1", body)

    def test_delta_with_only_bridge_appended(self):
        self._write(
            main="2026-04-29 10:00:00,000 [INFO] m1\n",
            bridge="",
        )
        first = self.client.get(self.url, data={"tail": 100})
        main_off = int(first["X-Log-Size"])
        bridge_off = int(first["X-Bridge-Log-Size"])
        # Bridge writes its first line AFTER the initial tail.
        self.bridge_path.write_text(
            "2026-04-29 10:00:05,000 [INFO] late-bridge\n", encoding="utf-8",
        )
        delta = self.client.get(self.url, data={"start": main_off, "bridge_start": bridge_off})
        body = delta.content.decode("utf-8")
        self.assertIn("[bridge] 2026-04-29 10:00:05,000 [INFO] late-bridge", body)
        # Main file unchanged → no main delta.
        self.assertNotIn("[INFO] m1", body)
        # X-Log-Size unchanged; X-Bridge-Log-Size grew.
        self.assertEqual(int(delta["X-Log-Size"]), main_off)
        self.assertGreater(int(delta["X-Bridge-Log-Size"]), bridge_off)

    def test_missing_bridge_file_is_treated_as_empty(self):
        # Bridge has never run for this pipeline — file does not exist.
        self.main_path.write_text(
            "2026-04-29 10:00:00,000 [INFO] only-main\n", encoding="utf-8",
        )
        if self.bridge_path.exists():
            self.bridge_path.unlink()
        resp = self.client.get(self.url, data={"tail": 100})
        body = resp.content.decode("utf-8")
        self.assertIn("[INFO] only-main", body)
        self.assertNotIn("[bridge]", body)
        self.assertEqual(int(resp["X-Bridge-Log-Size"]), 0)
