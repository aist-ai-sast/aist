"""
No container environment carries ``CLAUDE_CODE_OAUTH_TOKEN`` at boot.

The token reaches the ``claude`` subprocess per request only: ``aist.tasks.sast_execution`` /
``aist.tasks.claude`` → BridgeClient → bridge ``subprocess_env``. A token in ``docker-compose.yml`` would give
every organization one shared Claude account.

``aist/test/test_claude_integration_architecture.py`` keeps the literal out of the source tree. The unit-test image
does not include ``docker-compose.yml`` or ``tools/aist-triage-bridge/``; each test is skipped with a marker where
those files are absent and asserts where they are present.
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
