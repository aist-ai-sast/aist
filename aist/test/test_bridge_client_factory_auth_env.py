"""
Task 6 of docs/plans/2026-05-12-claude-as-org-integration.md.

The factory ``build_bridge_client_from_settings`` accepts a generic
``auth_env`` kwarg and forwards it to ``BridgeClient``. Per architectural
invariant I4, the factory itself MUST NOT resolve integrations or
touch the DB — that responsibility lives at the call site (typically
the generic pipeline execution task / ``aist.tasks.claude``).
"""
from __future__ import annotations

from django.test import TestCase

from aist.utils.bridge_client_factory import build_bridge_client_from_settings


class BridgeClientFactoryAuthEnvTests(TestCase):

    def test_factory_returns_client_with_auth_env(self):
        client = build_bridge_client_from_settings(
            auth_env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-abc"},
        )
        # Touch private attr — the factory is the boundary that should
        # surface this, and other code reads it via BridgeClient's
        # public methods (analyze_sync). We assert internal storage here
        # to make the wiring explicit without needing a network roundtrip.
        self.assertEqual(
            client._auth_env,
            {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-abc"},
        )

    def test_factory_default_auth_env_is_empty(self):
        client = build_bridge_client_from_settings()
        self.assertEqual(client._auth_env, {})

    def test_factory_does_not_call_db(self):
        # I4 — factory is a thin settings-only constructor. Adding a
        # DB query here would couple infrastructure setup to runtime
        # request context and break testability.
        with self.assertNumQueries(0):
            build_bridge_client_from_settings(
                auth_env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-irrelevant"},
            )

    def test_factory_does_not_import_claude_module(self):
        # I4 + I1 — the factory must not know about Claude at the import
        # level. Docstring references that point readers to the
        # appropriate caller-side module are fine; an actual ``import``
        # would couple the factory to a specific agent and break I4.
        import ast  # noqa: PLC0415
        import inspect  # noqa: PLC0415

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
                f"factory must not import {name!r} (architectural invariant I4)",
            )
