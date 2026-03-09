"""
Tests for pipeline workspace path helpers (get_project_build_path, cleanup_project_build_path).

User scenario: each pipeline run gets an isolated workspace directory; after the run
the directory is cleaned up so per-run dirs do not accumulate on disk.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from aist.utils.pipeline import BUILD_DIR_WARNING, cleanup_project_build_path, get_project_build_path


class GetProjectBuildPathTests(SimpleTestCase):
    def _path(self, base, name, version, pipeline_id):
        return str(Path(base) / name / version / "runs" / pipeline_id)

    def test_returns_isolated_run_path(self):
        with self.settings(AIST_PROJECTS_BUILD_DIR="/builds"):
            result = get_project_build_path("myproject", "master", "abc123")
        self.assertEqual(result, self._path("/builds", "myproject", "master", "abc123"))

    def test_different_pipeline_ids_yield_different_paths(self):
        with self.settings(AIST_PROJECTS_BUILD_DIR="/builds"):
            p1 = get_project_build_path("proj", "v1", "run-1")
            p2 = get_project_build_path("proj", "v1", "run-2")
        self.assertNotEqual(p1, p2)

    def test_same_pipeline_id_is_deterministic(self):
        with self.settings(AIST_PROJECTS_BUILD_DIR="/builds"):
            p1 = get_project_build_path("proj", "v1", "same-id")
            p2 = get_project_build_path("proj", "v1", "same-id")
        self.assertEqual(p1, p2)

    def test_fallback_names_when_empty(self):
        with self.settings(AIST_PROJECTS_BUILD_DIR="/builds"):
            result = get_project_build_path("", "", "pipe-x")
        self.assertEqual(result, self._path("/builds", "project", "default", "pipe-x"))

    def test_raises_when_build_dir_not_configured(self):
        with self.settings(AIST_PROJECTS_BUILD_DIR=None), self.assertRaises(RuntimeError) as ctx:
            get_project_build_path("proj", "v1", "pipe-x")
        self.assertIn(BUILD_DIR_WARNING, str(ctx.exception))


class CleanupProjectBuildPathTests(SimpleTestCase):
    def test_removes_run_directory(self):
        with tempfile.TemporaryDirectory() as base:
            run_dir = Path(base) / "proj" / "v1" / "runs" / "pipe-x"
            run_dir.mkdir(parents=True)
            (run_dir / "file.txt").write_text("data")

            with self.settings(AIST_PROJECTS_BUILD_DIR=base):
                cleanup_project_build_path("proj", "v1", "pipe-x")

            self.assertFalse(run_dir.exists())
            # Parent dirs (project / version) must remain intact
            self.assertTrue((Path(base) / "proj" / "v1" / "runs").exists())

    def test_does_not_raise_when_directory_missing(self):
        with tempfile.TemporaryDirectory() as base, self.settings(AIST_PROJECTS_BUILD_DIR=base):
            # Should not raise even if run dir was never created
            cleanup_project_build_path("proj", "v1", "nonexistent-pipe")

    def test_does_not_raise_when_build_dir_not_configured(self):
        with self.settings(AIST_PROJECTS_BUILD_DIR=None):
            # Should silently return, not raise
            cleanup_project_build_path("proj", "v1", "pipe-x")

    def test_does_not_remove_sibling_run_directories(self):
        with tempfile.TemporaryDirectory() as base:
            runs = Path(base) / "proj" / "v1" / "runs"
            (runs / "pipe-keep").mkdir(parents=True)
            (runs / "pipe-remove").mkdir(parents=True)

            with self.settings(AIST_PROJECTS_BUILD_DIR=base):
                cleanup_project_build_path("proj", "v1", "pipe-remove")

            self.assertFalse((runs / "pipe-remove").exists())
            self.assertTrue((runs / "pipe-keep").exists())
