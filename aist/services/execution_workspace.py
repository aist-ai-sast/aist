"""Workspace preparation and retention for one project's executions."""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db import transaction

from aist.services.pipeline_lifecycle import pipelines_with_released_sources

_logger = logging.getLogger(__name__)
BUILD_DIR_WARNING = "AIST_PROJECTS_BUILD_DIR is not set"


@dataclass(frozen=True, slots=True)
class ExecutionWorkspace:
    project_id: int
    project_name: str
    version: str

    def prepare_execution(self, pipeline_id: str) -> tuple[Path, Path]:
        """Prepare the common workspace and durable result root for one pipeline run."""
        workspace = self._workspace_path(pipeline_id)
        self._cleanup_terminal_workspaces(keep_pipeline_id=pipeline_id)
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace, self._output_path(pipeline_id)

    def cleanup_workspace(self, pipeline_id: str) -> None:
        """Remove one pipeline workspace without touching its durable results."""
        if not getattr(settings, "AIST_PROJECTS_BUILD_DIR", None):
            return
        try:
            workspace = self._workspace_path(pipeline_id)
            if workspace.exists():
                shutil.rmtree(workspace)
                _logger.info("Cleaned up pipeline workspace: %s", workspace)
        except Exception:
            _logger.exception("Failed to clean up pipeline workspace (pipeline_id=%s)", pipeline_id)

    def _workspace_path(self, pipeline_id: str) -> Path:
        configured = getattr(settings, "AIST_PROJECTS_BUILD_DIR", None)
        if not configured:
            raise RuntimeError(BUILD_DIR_WARNING)
        return self._path_within(
            Path(configured),
            self.project_name or "project",
            self.version or "default",
            "runs",
            pipeline_id,
        )

    def _output_path(self, pipeline_id: str) -> Path:
        configured = getattr(settings, "AIST_OUTPUT_PATH", None)
        root = Path(configured) if configured else Path(tempfile.gettempdir()) / "aist" / "output"
        return self._path_within(
            root,
            self.project_name or "project",
            self.version or "default",
            pipeline_id,
        )

    @staticmethod
    def _path_within(root: Path, *parts: str) -> Path:
        resolved_root = root.resolve()
        resolved = resolved_root.joinpath(*parts).resolve()
        try:
            resolved.relative_to(resolved_root)
        except ValueError:
            msg = f"Pipeline path escapes configured root: {resolved}"
            raise ValueError(msg)
        return resolved

    def _cleanup_terminal_workspaces(self, *, keep_pipeline_id: str) -> None:
        released = pipelines_with_released_sources().filter(project_id=self.project_id).exclude(id=keep_pipeline_id)
        for pipeline_id in list(released.values_list("id", flat=True)):
            with transaction.atomic():
                if released.select_for_update(skip_locked=True).filter(pk=pipeline_id).exists():
                    self.cleanup_workspace(pipeline_id)
