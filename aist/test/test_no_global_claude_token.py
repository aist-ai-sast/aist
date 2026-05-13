"""
Task 9 — ensure ``CLAUDE_CODE_OAUTH_TOKEN`` is not embedded in any
container env at boot time.

After the integration refactor, the token MUST flow per-request from
``aist.tasks.pipeline`` / ``aist.tasks.claude`` → BridgeClient → bridge
``subprocess_env`` → claude subprocess only. A stale env var in
``docker-compose.yml`` would silently re-introduce a global Claude
account shared by every organization — defeats the whole point of
``OrgIntegration(type=CLAUDE_CODE)``.

This file complements Task 14's source-tree grep invariant. The
Django unit-tests image bakes only ``aist/``, ``aist_site/``,
``sast-combinator/`` and ``vendor/`` — it does NOT include
``docker-compose.yml`` or ``tools/aist-triage-bridge/`` files. When
those files are not visible (the normal CI test-run path), each test
is skipped with a clear marker. When the test suite is invoked from a
full repo checkout (e.g. via a future Dockerfile that mounts the
root, or by running pytest outside the image), the tests turn into
real assertions. Reviewers should still manually verify these files
during PR review.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from django.test import TestCase

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _read_or_skip(rel_path: str) -> str:
    path = _REPO_ROOT / rel_path
    if not path.exists():
        msg = (
            f"{rel_path} not visible inside the test container; skipping "
            "architectural file check. See Task 14 grep invariant for "
            "source-tree-wide enforcement."
        )
        raise unittest.SkipTest(msg)
    return path.read_text(encoding="utf-8")


class NoGlobalClaudeTokenTests(TestCase):

    def test_docker_compose_has_no_global_claude_token(self):
        compose = _read_or_skip("docker-compose.yml")
        self.assertNotIn(
            "CLAUDE_CODE_OAUTH_TOKEN",
            compose,
            "docker-compose.yml must not declare a global Claude token — "
            "tokens flow per-request via OrgIntegration. See Task 9.",
        )

    def test_bridge_entrypoint_has_no_token_env_check(self):
        entrypoint = _read_or_skip("tools/aist-triage-bridge/entrypoint.sh")
        self.assertNotIn(
            "CLAUDE_CODE_OAUTH_TOKEN",
            entrypoint,
            "Bridge entrypoint must not require CLAUDE_CODE_OAUTH_TOKEN — "
            "the bridge accepts the token per-request via AnalyzeRequest.",
        )

    def test_bridge_main_does_not_reference_token_env_var(self):
        # main.py operates on the generic ``subprocess_env`` field. The
        # literal env-var name must NOT appear in bridge source — its
        # semantics live in aist/integrations/claude.py (I1).
        main = _read_or_skip("tools/aist-triage-bridge/main.py")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", main)
