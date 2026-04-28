"""
Shared runtime-config builder for ``type: agent-bridge`` analyzers.

The bridge writes the returned dict to a JSON sidecar (``<analyzer>_runtime.json``)
that every agent skill reads at start. Keys are a union of everything any
current agent skill might need; each skill reads its own subset and ignores
the rest. This keeps the builder analyzer-agnostic and lets new agents land
without growing analyzer-name branches in ``run_sast_pipeline``.

Per-project overrides live in ``AISTProject.profile.agent_analyzers``;
Django settings provide the defaults.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from aist.profile import ProjectProfile

from .diff_baseline import _positive_int_setting, get_prior_successful_commit

if TYPE_CHECKING:
    from aist.models import AISTPipeline


def build_agent_runtime_env(pipeline: AISTPipeline) -> dict[str, str]:
    """
    Return the runtime sidecar dict for any agent-bridge analyzer.

    All values are stringified so the sidecar JSON encoding is uniform.
    """
    profile = ProjectProfile.from_dict(pipeline.project.profile)
    excluded = profile.get_excluded_paths()
    full_limits = profile.get_full_security_limits()

    return {
        # --- Shared / diff-skill keys (claude-diff-security) --- #
        "BASE_COMMIT": get_prior_successful_commit(pipeline) or "",
        "EXCLUDED_PATHS_JSON": json.dumps(excluded),
        "CLAUDE_DIFF_MAX_FILES": str(_positive_int_setting("CLAUDE_DIFF_MAX_FILES")),
        "CLAUDE_DIFF_MAX_BYTES": str(_positive_int_setting("CLAUDE_DIFF_MAX_BYTES")),
        # --- Full-skill keys (claude-full-security) --- #
        "AGENT_FULL_MAX_FILES": str(
            full_limits.max_files
            if full_limits.max_files is not None
            else _positive_int_setting("AGENT_FULL_MAX_FILES"),
        ),
        "AGENT_FULL_MAX_BYTES": str(
            full_limits.max_bytes
            if full_limits.max_bytes is not None
            else _positive_int_setting("AGENT_FULL_MAX_BYTES"),
        ),
        "AGENT_FULL_MAX_FILE_BYTES": str(
            full_limits.max_file_bytes
            if full_limits.max_file_bytes is not None
            else _positive_int_setting("AGENT_FULL_MAX_FILE_BYTES"),
        ),
        "AGENT_FULL_MAX_FINDINGS": str(
            full_limits.max_findings
            if full_limits.max_findings is not None
            else _positive_int_setting("AGENT_FULL_MAX_FINDINGS"),
        ),
    }
