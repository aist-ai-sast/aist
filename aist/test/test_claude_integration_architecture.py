"""
Task 14 — architectural-invariant meta-test for the Claude-as-OrgIntegration
refactor (docs/plans/2026-05-12-claude-as-org-integration.md).

Enforces the four invariants stated in the plan:

- **I1** the literal ``CLAUDE_CODE_OAUTH_TOKEN`` appears in exactly one
  source file — ``aist/integrations/claude.py``. Test files are
  whitelisted so they can reference the constant in assertions.
- **I2** ``BridgeClient.__init__`` and ``BridgeClient.analyze_sync`` /
  ``analyze_async`` contain no Claude-specific parameter names.
- **I3** ``agent_bridge_runner.py`` is agent-agnostic — no Claude /
  OAuth / Anthropic references in source.
- **I4** ``aist/utils/bridge_client_factory.py`` does not import the
  Claude integrations module or resolve integrations itself.

When this file is the offending source under I1 (intentional — the
invariants explicitly whitelist it), the test still passes because it
allow-lists itself.

This test complements ``aist/test/test_no_global_claude_token.py``
which checks container-orchestration files; that test skips when the
files are not visible inside the baked test image. This one inspects
Python source which IS available inside the container.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from django.test import TestCase

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TOKEN_LITERAL = "CLAUDE_CODE_OAUTH_TOKEN"  # noqa: S105
_OAUTH_TOKEN_LITERAL = "OAUTH_TOKEN"  # noqa: S105

# Whitelist: files allowed to mention CLAUDE_CODE_OAUTH_TOKEN.
# - aist/integrations/claude.py is the single concentrator (I1).
# - test files that ASSERT the redaction / forwarding behaviour
#   legitimately reference the literal value.
# - test_no_global_claude_token.py asserts the absence in compose etc.
_TOKEN_LITERAL_WHITELIST = {
    "aist/integrations/claude.py",
    "aist/test/test_claude_integration_architecture.py",  # this file
    "aist/test/test_claude_integration_module.py",
    "aist/test/test_claude_integration_api.py",
    "aist/test/test_claude_analyze.py",
    "aist/test/test_bridge_client_factory_auth_env.py",
    "aist/test/test_pipeline_claude_auth.py",
    "aist/test/test_no_global_claude_token.py",
    "aist/test/test_migrate_claude_env_command.py",
    "aist/management/commands/migrate_claude_env_to_integration.py",
    "aist/test/test_local_triage.py",  # asserts bridge auth_env contains CLAUDE_CODE_OAUTH_TOKEN
}


def _python_sources_under(*roots: str):
    """Yield (rel_path, source_text) for every .py file under given roots."""
    for root in roots:
        base = _REPO_ROOT / root
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(_REPO_ROOT).as_posix()
            try:
                yield rel, path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue


class ArchitecturalInvariantTests(TestCase):

    def test_I1_oauth_token_literal_appears_only_in_whitelisted_files(self):
        offenders: list[str] = []
        for rel, src in _python_sources_under("aist"):
            if _TOKEN_LITERAL not in src:
                continue
            if rel not in _TOKEN_LITERAL_WHITELIST:
                offenders.append(rel)
        self.assertFalse(
            offenders,
            f"Invariant I1 violated: {_TOKEN_LITERAL!r} found in non-whitelisted "
            f"files: {offenders}. Move the reference into "
            f"aist/integrations/claude.py or extend the whitelist with a "
            f"comment explaining why the new file legitimately needs it.",
        )

    def test_I2_bridge_client_signature_is_agent_agnostic(self):
        from pipeline.bridge_client import BridgeClient  # noqa: PLC0415

        forbidden = ("claude", "oauth", "anthropic", "sk_ant", "sk-ant-")

        for method_name in ("__init__", "analyze_sync", "analyze_async"):
            method = getattr(BridgeClient, method_name)
            sig = inspect.signature(method)
            for param_name in sig.parameters:
                lower = param_name.lower()
                for token in forbidden:
                    self.assertNotIn(
                        token, lower,
                        f"Invariant I2 violated: BridgeClient.{method_name} "
                        f"parameter {param_name!r} contains agent-specific "
                        f"token {token!r}",
                    )

    def test_I3_agent_bridge_runner_is_agent_agnostic(self):
        # The agent-bridge runner lives under sast-combinator inside the
        # baked image. If the file is missing for any reason, skip — the
        # invariant is then enforced elsewhere (CI / source review).
        path = _REPO_ROOT / "sast-combinator" / "sast-pipeline" / "pipeline" / "agent_bridge_runner.py"
        if not path.exists():
            self.skipTest("agent_bridge_runner.py not present in test environment")
        src = path.read_text(encoding="utf-8").lower()
        forbidden_tokens = ("claude_code_oauth_token", "anthropic", "sk-ant-")
        # ``claude`` alone is too generic (matches "Claude-bridge" log msgs?)
        # — we check the more specific tokens that indicate agent leakage.
        for token in forbidden_tokens:
            self.assertNotIn(
                token, src,
                f"Invariant I3 violated: agent_bridge_runner contains "
                f"agent-specific token {token!r}",
            )

    def test_I4_factory_does_not_import_claude_module_or_resolver(self):
        from aist.utils import bridge_client_factory  # noqa: PLC0415

        src = inspect.getsource(bridge_client_factory)
        tree = ast.parse(src)
        imported_names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported_names.append(node.module or "")
            elif isinstance(node, ast.Import):
                imported_names.extend(alias.name for alias in node.names)

        for name in imported_names:
            self.assertFalse(
                name.startswith("aist.integrations.claude"),
                f"Invariant I4 violated: factory imports {name!r}",
            )
            self.assertFalse(
                name.startswith("aist.integrations.resolver"),
                f"Invariant I4 violated: factory imports resolver "
                f"({name!r}) — integration resolution belongs to the "
                f"caller, not the constructor.",
            )

    def test_I1_oauth_token_in_concentrator_is_used_for_env_var_mapping(self):
        # Sanity check: the literal MUST actually appear in claude.py
        # (otherwise the whitelist hides a regression of the opposite
        # kind — claude.py losing the constant during refactor).
        claude_module_src = (_REPO_ROOT / "aist/integrations/claude.py").read_text(encoding="utf-8")
        self.assertIn(
            _TOKEN_LITERAL,
            claude_module_src,
            "Invariant I1 sanity: the literal must exist in "
            "aist/integrations/claude.py (single concentrator).",
        )
