"""
Lint gate — forbid re-deriving "does this DAST launch need a source repository" from raw
field truthiness anywhere outside the single designated accessor.

`DastProjectBinding.requires_source_repository` (`aist/models.py`) is the only place that
answers this question; every other module must ask it rather than testing
`source_repo_key`/`repository_keys` truthiness/length directly (see the accessor's own
docstring). A second, independent re-derivation is exactly how the "sourceless DAST target"
invariant would silently drift out of sync — one call site relaxed for the sourceless case,
another still gated on the old raw-field check.

Two files are exempt because they originate the invariant rather than consuming it:

- `aist/models.py` — defines `requires_source_repository` and enforces the field-level
  invariant that the property depends on.
- `aist/integrations/dast_config.py` — defines `DastLaunchRequirements.requires_repository()`
  and is the one place that validates `repository_keys` against it when parsing a wire
  snapshot.
- `aist/integrations/dast_report.py` — a deliberately model-decoupled trust-boundary layer;
  it never sees a `DastProjectBinding`, only a plain `frozenset[str]` of repository keys its
  caller already resolved via the accessor, so it has no accessor to call.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.test import SimpleTestCase

_AIST_DIR = Path(__file__).resolve().parent.parent

_EXEMPT_FILES = frozenset({
    "models.py",
    "integrations/dast_config.py",
    "integrations/dast_report.py",
})

_EXEMPT_DIR_PREFIXES = ("test/", "migrations/")

# Bare truthiness/length checks on the raw fields, in the shapes that stand in for
# "does this binding/target require a source repository":
#   if x.source_repo_key:            elif not x.repository_keys:
#   ... if not x.source_repo_key else ...
#   len(x.repository_keys) == 0      bool(x.source_repo_key)
_FORBIDDEN = re.compile(
    r"\b(?:el)?if\s+(?:not\s+)?[\w.]*\.(?:source_repo_key|repository_keys)\s*:|"
    r"if\s+(?:not\s+)?[\w.]*\.(?:source_repo_key|repository_keys)\s+else\b|"
    r"\blen\(\s*[\w.]*\.repository_keys\s*\)|"
    r"\bbool\(\s*[\w.]*\.(?:source_repo_key|repository_keys)\s*\)",
)


def _relative_path(path: Path) -> str:
    return path.relative_to(_AIST_DIR).as_posix()


def _lint_python_files():
    for path in _AIST_DIR.rglob("*.py"):
        rel = _relative_path(path)
        if rel in _EXEMPT_FILES or any(rel.startswith(prefix) for prefix in _EXEMPT_DIR_PREFIXES):
            continue
        yield path, rel


def _files_with_violations() -> set[str]:
    offenders = set()
    for path, rel in _lint_python_files():
        if _FORBIDDEN.search(path.read_text(encoding="utf-8")):
            offenders.add(rel)
    return offenders


class DastSourceRequirementLintTests(SimpleTestCase):

    def test_no_raw_source_requirement_checks_outside_the_accessor(self):
        offenders = _files_with_violations()
        self.assertEqual(
            offenders,
            set(),
            "Found a raw source_repo_key/repository_keys truthiness or length check outside "
            f"the designated accessor in: {sorted(offenders)}. Use "
            "`DastProjectBinding.requires_source_repository` (or "
            "`DastLaunchRequirements.requires_repository()` if you don't have a binding) "
            "instead of re-deriving the answer from raw fields.",
        )

    def test_exempt_files_still_exist(self):
        for rel in _EXEMPT_FILES:
            self.assertTrue(
                (_AIST_DIR / rel).is_file(),
                f"Exempt file {rel} no longer exists — remove it from _EXEMPT_FILES.",
            )
