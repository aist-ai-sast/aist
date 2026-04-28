"""
Tests for the shared agent-analyzer runtime config builder.

``build_agent_runtime_env`` is the single source of truth for the JSON sidecar
that the bridge writes for every ``type: agent-bridge`` analyzer. The dict is
the union of keys all current agent skills might need; each skill reads its
own subset and ignores the rest. Profile overrides on
``agent_analyzers.full_security`` take precedence over Django settings
defaults.
"""
from __future__ import annotations

import json

from aist.models import AISTPipeline, AISTProjectVersion, AISTStatus, VersionType
from aist.test.test_api import AISTApiBase
from aist.utils.agent_runtime import build_agent_runtime_env

DIFF_KEYS = ("BASE_COMMIT", "EXCLUDED_PATHS_JSON", "CLAUDE_DIFF_MAX_FILES", "CLAUDE_DIFF_MAX_BYTES")
FULL_KEYS = (
    "AGENT_FULL_MAX_FILES",
    "AGENT_FULL_MAX_BYTES",
    "AGENT_FULL_MAX_FILE_BYTES",
    "AGENT_FULL_MAX_FINDINGS",
)
ALL_KEYS = DIFF_KEYS + FULL_KEYS


class BuildAgentRuntimeEnvTests(AISTApiBase):

    def setUp(self):
        super().setUp()
        self.project.profile = {"paths": {"exclude": ["vendor/", "third_party/"]}}
        self.project.save(update_fields=["profile"])

        self.branch_pv = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="main",
        )
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-build-agent-env",
            project=self.project,
            project_version=self.branch_pv,
            status=AISTStatus.SAST_LAUNCHED,
        )

    # ------------------------------------------------------------------ #
    # Shape — all keys present, all string values                          #
    # ------------------------------------------------------------------ #

    def test_returns_union_of_diff_and_full_keys(self):
        env = build_agent_runtime_env(self.pipeline)
        for key in ALL_KEYS:
            self.assertIn(key, env, msg=key)
            self.assertIsInstance(env[key], str, msg=key)

    def test_excluded_paths_shared_between_skills(self):
        env = build_agent_runtime_env(self.pipeline)
        decoded = json.loads(env["EXCLUDED_PATHS_JSON"])
        self.assertEqual(sorted(decoded), ["third_party/", "vendor/"])

    def test_base_commit_empty_when_no_prior_pipeline(self):
        # Full skill ignores BASE_COMMIT, but the key must still be present so
        # diff and full can share the same builder without conditional plumbing.
        env = build_agent_runtime_env(self.pipeline)
        self.assertEqual(env["BASE_COMMIT"], "")

    # ------------------------------------------------------------------ #
    # Django defaults                                                       #
    # ------------------------------------------------------------------ #

    def test_full_keys_default_to_django_settings(self):
        with self.settings(
            AGENT_FULL_MAX_FILES=111,
            AGENT_FULL_MAX_BYTES=222,
            AGENT_FULL_MAX_FILE_BYTES=333,
            AGENT_FULL_MAX_FINDINGS=44,
        ):
            env = build_agent_runtime_env(self.pipeline)
        self.assertEqual(env["AGENT_FULL_MAX_FILES"], "111")
        self.assertEqual(env["AGENT_FULL_MAX_BYTES"], "222")
        self.assertEqual(env["AGENT_FULL_MAX_FILE_BYTES"], "333")
        self.assertEqual(env["AGENT_FULL_MAX_FINDINGS"], "44")

    def test_diff_keys_default_to_django_settings(self):
        with self.settings(CLAUDE_DIFF_MAX_FILES=17, CLAUDE_DIFF_MAX_BYTES=4096):
            env = build_agent_runtime_env(self.pipeline)
        self.assertEqual(env["CLAUDE_DIFF_MAX_FILES"], "17")
        self.assertEqual(env["CLAUDE_DIFF_MAX_BYTES"], "4096")

    def test_invalid_full_setting_is_rejected(self):
        with self.settings(AGENT_FULL_MAX_FILES=0), self.assertRaises(ValueError):
            build_agent_runtime_env(self.pipeline)

    # ------------------------------------------------------------------ #
    # Profile overrides                                                     #
    # ------------------------------------------------------------------ #

    def test_full_profile_overrides_take_precedence_over_django_defaults(self):
        self.project.profile = {
            "agent_analyzers": {
                "full_security": {
                    "max_files": 9,
                    "max_bytes": 99,
                    "max_file_bytes": 999,
                    "max_findings": 7,
                },
            },
        }
        self.project.save(update_fields=["profile"])

        with self.settings(
            AGENT_FULL_MAX_FILES=111,
            AGENT_FULL_MAX_BYTES=222,
            AGENT_FULL_MAX_FILE_BYTES=333,
            AGENT_FULL_MAX_FINDINGS=44,
        ):
            env = build_agent_runtime_env(self.pipeline)

        self.assertEqual(env["AGENT_FULL_MAX_FILES"], "9")
        self.assertEqual(env["AGENT_FULL_MAX_BYTES"], "99")
        self.assertEqual(env["AGENT_FULL_MAX_FILE_BYTES"], "999")
        self.assertEqual(env["AGENT_FULL_MAX_FINDINGS"], "7")

    def test_partial_profile_override_falls_back_per_field(self):
        self.project.profile = {
            "agent_analyzers": {"full_security": {"max_files": 9}},
        }
        self.project.save(update_fields=["profile"])

        with self.settings(
            AGENT_FULL_MAX_FILES=111,
            AGENT_FULL_MAX_BYTES=222,
            AGENT_FULL_MAX_FILE_BYTES=333,
            AGENT_FULL_MAX_FINDINGS=44,
        ):
            env = build_agent_runtime_env(self.pipeline)

        # Profile overrides win for the field it sets…
        self.assertEqual(env["AGENT_FULL_MAX_FILES"], "9")
        # …and Django defaults fill in the rest.
        self.assertEqual(env["AGENT_FULL_MAX_BYTES"], "222")
        self.assertEqual(env["AGENT_FULL_MAX_FILE_BYTES"], "333")
        self.assertEqual(env["AGENT_FULL_MAX_FINDINGS"], "44")

    def test_full_profile_override_does_not_affect_diff_keys(self):
        # Diff and full live under separate config sections; an override on
        # one must never leak into the other.
        self.project.profile = {
            "paths": {"exclude": ["vendor/"]},
            "agent_analyzers": {"full_security": {"max_files": 9}},
        }
        self.project.save(update_fields=["profile"])

        with self.settings(CLAUDE_DIFF_MAX_FILES=17, CLAUDE_DIFF_MAX_BYTES=4096):
            env = build_agent_runtime_env(self.pipeline)

        self.assertEqual(env["CLAUDE_DIFF_MAX_FILES"], "17")
        self.assertEqual(env["CLAUDE_DIFF_MAX_BYTES"], "4096")
        self.assertEqual(env["AGENT_FULL_MAX_FILES"], "9")


class BuildDiffEnvBackwardCompatTests(AISTApiBase):

    """
    ``build_diff_env`` must remain importable and behave as a thin alias for
    ``build_agent_runtime_env`` so that ``aist.tasks.pipeline`` and the
    existing test suite keep working through Task 3.
    """

    def setUp(self):
        super().setUp()
        self.branch_pv = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="main",
        )
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-back-compat",
            project=self.project,
            project_version=self.branch_pv,
            status=AISTStatus.SAST_LAUNCHED,
        )

    def test_build_diff_env_returns_same_dict_as_unified_builder(self):
        from aist.utils.diff_baseline import build_diff_env  # noqa: PLC0415
        self.assertEqual(build_diff_env(self.pipeline), build_agent_runtime_env(self.pipeline))
