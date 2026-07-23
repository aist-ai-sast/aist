"""
Lint gate — forbid the patterns that let an AIST endpoint dodge the central layer.

Two forbidden forms in ``aist/api/*.py``:

1. Transforming or indexing ``request.data`` directly — raw request body not run through a serializer
   (AGENTS.md "no request.data accessed directly in views"). This is exactly the
   shape behind the unvalidated AI-callback payload (G-5).
2. ``get_object_or_404(<OrgOwnedModel>, ...)`` — object resolution that bypasses the
   tenant-scoped ``aist.queries`` getters (the root pattern behind G-3). Object
   lookups must go through ``AISTAPIView.resolve``.

The allowlist is intentionally empty; the test fails if a new violation is introduced.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.test import SimpleTestCase

_API_DIR = Path(__file__).resolve().parent.parent / "api"

# Org-owned models that must be resolved through the scoped getters, never raw.
_ORG_OWNED_MODELS = (
    "AISTPipeline", "AISTProject", "AISTProjectVersion", "AISTProjectLaunchConfig",
    "AISTLaunchConfigAction", "LaunchSchedule", "PipelineLaunchQueue", "Finding",
    "OrgIntegration", "WorkItemProvider", "ProjectIntegrationOverride",
)

_RAW_REQUEST_DATA = re.compile(
    r"\bdict\(\s*request\.data\s*\)|request\.data\.(?:get|copy)\s*\(|"
    r"request\.data\s*\[|\{\s*\*\*request\.data",
)
_RAW_LOOKUP = re.compile(
    rf"get_object_or_404\(\s*(?:{'|'.join(_ORG_OWNED_MODELS)})\b",
)

KNOWN_VIOLATIONS = set()


def _files_with_violations() -> set[str]:
    offenders = set()
    for path in _API_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if _RAW_REQUEST_DATA.search(text) or _RAW_LOOKUP.search(text):
            offenders.add(path.name)
    return offenders


class AuthzLintTests(SimpleTestCase):

    def test_no_new_forbidden_patterns(self):
        offenders = _files_with_violations()
        unexpected = offenders - KNOWN_VIOLATIONS
        self.assertEqual(
            unexpected, set(),
            "New forbidden authz pattern (raw request.data transformation or get_object_or_404 "
            f"on an org-owned model) in: {sorted(unexpected)}. Resolve via AISTAPIView + "
            "a serializer.",
        )

    def test_allowlist_has_no_stale_entries(self):
        offenders = _files_with_violations()
        stale = KNOWN_VIOLATIONS - offenders
        self.assertEqual(
            stale, set(),
            f"These files are clean now — remove them from KNOWN_VIOLATIONS: {sorted(stale)}",
        )
