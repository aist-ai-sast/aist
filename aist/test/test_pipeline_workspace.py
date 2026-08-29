"""
Tests for the common execution paths owned by PipelineArguments.

User scenario: each pipeline run gets an isolated workspace directory; after the run
the directory is cleaned up so per-run dirs do not accumulate on disk.
"""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from aist.models import AISTPipeline, AISTStatus
from aist.pipeline_args import BUILD_DIR_WARNING, PipelineArguments, SastPipelineArguments
from aist.test.test_api import AISTApiBase


class PipelineExecutionPathTests(AISTApiBase):
    def _arguments(self, *, version=None):
        return PipelineArguments(
            project=self.project,
            payload=SastPipelineArguments(
                project=self.project,
                project_version={"version": self.pv.version if version is None else version},
            ),
        )

    def test_returns_isolated_run_path(self):
        with self.settings(AIST_PROJECTS_BUILD_DIR="/builds"):
            workspace, _output = self._arguments(version="master").prepare_execution("abc123")
        self.assertEqual(workspace, Path("/builds") / self.product.name / "master" / "runs" / "abc123")

    def test_sast_workspace_remains_traversable_by_the_separate_bridge_user(self):
        with tempfile.TemporaryDirectory() as build_dir:
            Path(build_dir).chmod(0o755)
            previous_umask = os.umask(0o022)
            try:
                with self.settings(AIST_PROJECTS_BUILD_DIR=build_dir):
                    workspace, _output = self._arguments().prepare_execution("pipeline-123")
            finally:
                os.umask(previous_umask)

            self.assertEqual(stat.S_IMODE(workspace.stat().st_mode), 0o755)

    def test_different_pipeline_ids_yield_different_paths(self):
        with self.settings(AIST_PROJECTS_BUILD_DIR="/builds"):
            arguments = self._arguments(version="v1")
            p1, _ = arguments.prepare_execution("run-1")
            p2, _ = arguments.prepare_execution("run-2")
        self.assertNotEqual(p1, p2)

    def test_same_pipeline_id_is_deterministic(self):
        with self.settings(AIST_PROJECTS_BUILD_DIR="/builds"):
            arguments = self._arguments(version="v1")
            p1, _ = arguments.prepare_execution("same-id")
            p2, _ = arguments.prepare_execution("same-id")
        self.assertEqual(p1, p2)

    def test_fallback_names_when_empty(self):
        self.product.name = ""
        with self.settings(AIST_PROJECTS_BUILD_DIR="/builds"):
            workspace, _ = self._arguments(version="").prepare_execution("pipe-x")
        self.assertEqual(workspace, Path("/builds/project/default/runs/pipe-x"))

    def test_raises_when_build_dir_not_configured(self):
        with self.settings(AIST_PROJECTS_BUILD_DIR=None), self.assertRaises(RuntimeError) as ctx:
            self._arguments().prepare_execution("pipe-x")
        self.assertIn(BUILD_DIR_WARNING, str(ctx.exception))

    def test_raises_on_path_traversal_in_project_name(self):
        self.product.name = "../../etc"
        with self.settings(AIST_PROJECTS_BUILD_DIR="/builds"), self.assertRaises(ValueError):
            self._arguments().prepare_execution("pipe-x")

    def test_raises_on_path_traversal_in_project_version(self):
        with self.settings(AIST_PROJECTS_BUILD_DIR="/builds"), self.assertRaises(ValueError):
            self._arguments(version="../../etc").prepare_execution("pipe-x")

    def test_raises_on_output_path_traversal(self):
        with (
            tempfile.TemporaryDirectory() as build_dir,
            tempfile.TemporaryDirectory() as output_dir,
            tempfile.TemporaryDirectory() as outside_dir,
        ):
            (Path(output_dir) / self.product.name).symlink_to(outside_dir, target_is_directory=True)
            with (
                self.settings(AIST_PROJECTS_BUILD_DIR=build_dir, AIST_OUTPUT_PATH=output_dir),
                self.assertRaises(ValueError),
            ):
                self._arguments().prepare_execution("pipe-x")

    def test_removes_run_directory(self):
        with tempfile.TemporaryDirectory() as base:
            run_dir = Path(base) / self.product.name / self.pv.version / "runs" / "pipe-x"
            run_dir.mkdir(parents=True)
            (run_dir / "file.txt").write_text("data")

            with self.settings(AIST_PROJECTS_BUILD_DIR=base):
                self._arguments().cleanup_workspace("pipe-x")

            self.assertFalse(run_dir.exists())
            self.assertTrue((Path(base) / self.product.name / self.pv.version / "runs").exists())

    def test_does_not_raise_when_directory_missing(self):
        with tempfile.TemporaryDirectory() as base, self.settings(AIST_PROJECTS_BUILD_DIR=base):
            self._arguments().cleanup_workspace("nonexistent-pipe")

    def test_does_not_raise_when_build_dir_not_configured(self):
        with self.settings(AIST_PROJECTS_BUILD_DIR=None):
            self._arguments().cleanup_workspace("pipe-x")

    def test_does_not_remove_sibling_run_directories(self):
        with tempfile.TemporaryDirectory() as base:
            runs = Path(base) / self.product.name / self.pv.version / "runs"
            (runs / "pipe-keep").mkdir(parents=True)
            (runs / "pipe-remove").mkdir(parents=True)

            with self.settings(AIST_PROJECTS_BUILD_DIR=base):
                self._arguments().cleanup_workspace("pipe-remove")

            self.assertFalse((runs / "pipe-remove").exists())
            self.assertTrue((runs / "pipe-keep").exists())

    def test_removes_only_terminal_sibling_run_directories(self):
        with tempfile.TemporaryDirectory() as base:
            runs = Path(base) / self.product.name / self.pv.version / "runs"
            for pipeline_id in ("pipe-keep-current", "pipe-keep-active", "pipe-drop-finished"):
                (runs / pipeline_id).mkdir(parents=True, exist_ok=True)

            AISTPipeline.objects.create(
                id="pipe-keep-current",
                project=self.project,
                project_version=self.pv,
                status=AISTStatus.FINISHED,
            )
            AISTPipeline.objects.create(
                id="pipe-keep-active",
                project=self.project,
                project_version=self.pv,
                status=AISTStatus.EXECUTING,
            )
            AISTPipeline.objects.create(
                id="pipe-drop-finished",
                project=self.project,
                project_version=self.pv,
                status=AISTStatus.FINISHED_WITH_WARNINGS,
            )

            with self.settings(AIST_PROJECTS_BUILD_DIR=base):
                self._arguments().prepare_execution("pipe-keep-current")

            self.assertTrue((runs / "pipe-keep-current").exists())
            self.assertTrue((runs / "pipe-keep-active").exists())
            self.assertFalse((runs / "pipe-drop-finished").exists())

    def test_concurrent_calls_do_not_raise(self):
        """
        Two pipeline starts race to clean up the same terminal workspace.
        The second call must silently succeed even though the directory is already gone.
        """
        with tempfile.TemporaryDirectory() as base:
            runs = Path(base) / self.product.name / self.pv.version / "runs"
            (runs / "pipe-done").mkdir(parents=True, exist_ok=True)

            AISTPipeline.objects.create(
                id="pipe-done",
                project=self.project,
                project_version=self.pv,
                status=AISTStatus.FINISHED,
            )

            with self.settings(AIST_PROJECTS_BUILD_DIR=base):
                arguments = self._arguments()
                arguments.prepare_execution("pipe-new-a")
                arguments.prepare_execution("pipe-new-b")

            self.assertFalse((runs / "pipe-done").exists())
