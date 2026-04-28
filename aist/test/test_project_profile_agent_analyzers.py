from __future__ import annotations

from aist.profile import FullSecurityLimits, ProjectProfile
from aist.test.test_api import AISTApiBase


class ProjectProfileAgentAnalyzersTests(AISTApiBase):

    """
    Locks the typed ``agent_analyzers.full_security`` extension of
    :class:`ProjectProfile` so the runtime-env builder (Task 3) can rely on it
    without re-implementing validation in tasks.
    """

    # ------------------------------------------------------------------ #
    # from_dict — defaults and parsing                                     #
    # ------------------------------------------------------------------ #

    def test_default_full_security_limits_are_unset(self):
        # No profile → all fields None so that callers fall back to Django settings.
        profile = ProjectProfile.from_dict(None)
        limits = profile.get_full_security_limits()
        self.assertEqual(limits, FullSecurityLimits())
        self.assertIsNone(limits.max_files)
        self.assertIsNone(limits.max_bytes)
        self.assertIsNone(limits.max_file_bytes)
        self.assertIsNone(limits.max_findings)

    def test_from_dict_parses_full_overrides(self):
        profile = ProjectProfile.from_dict({
            "agent_analyzers": {
                "full_security": {
                    "max_files": 1500,
                    "max_bytes": 8000000,
                    "max_file_bytes": 200000,
                    "max_findings": 50,
                },
            },
        })
        limits = profile.get_full_security_limits()
        self.assertEqual(limits.max_files, 1500)
        self.assertEqual(limits.max_bytes, 8000000)
        self.assertEqual(limits.max_file_bytes, 200000)
        self.assertEqual(limits.max_findings, 50)

    def test_from_dict_partial_overrides_leave_other_fields_none(self):
        profile = ProjectProfile.from_dict({
            "agent_analyzers": {"full_security": {"max_files": 200}},
        })
        limits = profile.get_full_security_limits()
        self.assertEqual(limits.max_files, 200)
        self.assertIsNone(limits.max_bytes)
        self.assertIsNone(limits.max_file_bytes)
        self.assertIsNone(limits.max_findings)

    def test_from_dict_ignores_unknown_keys(self):
        # Extra keys must not crash from_dict so old DB rows survive a schema bump.
        profile = ProjectProfile.from_dict({
            "agent_analyzers": {
                "full_security": {"max_files": 10, "unknown": "x"},
                "other_section": {"foo": 1},
            },
        })
        self.assertEqual(profile.get_full_security_limits().max_files, 10)

    def test_from_dict_handles_missing_full_security_subsection(self):
        profile = ProjectProfile.from_dict({"agent_analyzers": {}})
        self.assertEqual(profile.get_full_security_limits(), FullSecurityLimits())

    # ------------------------------------------------------------------ #
    # validate_dict — accept                                               #
    # ------------------------------------------------------------------ #

    def test_validate_dict_accepts_valid_full_overrides(self):
        ProjectProfile.validate_dict({
            "agent_analyzers": {
                "full_security": {
                    "max_files": 1500,
                    "max_bytes": 8000000,
                    "max_file_bytes": 200000,
                    "max_findings": 50,
                },
            },
        })

    def test_validate_dict_accepts_partial_overrides(self):
        ProjectProfile.validate_dict({
            "agent_analyzers": {"full_security": {"max_files": 1}},
        })

    def test_validate_dict_accepts_empty_agent_analyzers(self):
        ProjectProfile.validate_dict({"agent_analyzers": {}})
        ProjectProfile.validate_dict({"agent_analyzers": {"full_security": {}}})

    # ------------------------------------------------------------------ #
    # validate_dict — reject                                               #
    # ------------------------------------------------------------------ #

    def test_validate_dict_rejects_non_dict_agent_analyzers(self):
        with self.assertRaises(TypeError):
            ProjectProfile.validate_dict({"agent_analyzers": []})

    def test_validate_dict_rejects_non_dict_full_security(self):
        with self.assertRaises(TypeError):
            ProjectProfile.validate_dict({"agent_analyzers": {"full_security": []}})

    def test_validate_dict_rejects_zero_or_negative(self):
        for bad in (0, -1, -100):
            with self.assertRaises(ValueError, msg=f"bad value: {bad}"):
                ProjectProfile.validate_dict({
                    "agent_analyzers": {"full_security": {"max_files": bad}},
                })

    def test_validate_dict_rejects_non_integer(self):
        # String, float, None, and bool must all fail (bool is an int subclass in
        # Python — accepting it silently would let "max_files: true" through).
        for bad in ("100", 1.5, None, True, False):
            with self.assertRaises((TypeError, ValueError), msg=f"bad value: {bad!r}"):
                ProjectProfile.validate_dict({
                    "agent_analyzers": {"full_security": {"max_bytes": bad}},
                })

    def test_validate_dict_rejects_unknown_keys_in_full_security(self):
        # Reject typos so users notice when an override never applied.
        with self.assertRaises(ValueError):
            ProjectProfile.validate_dict({
                "agent_analyzers": {"full_security": {"max_filez": 100}},
            })

    def test_validate_dict_validates_each_field(self):
        # All four fields must be enforced — not only the first one we look up.
        for field_name in ("max_files", "max_bytes", "max_file_bytes", "max_findings"):
            with self.assertRaises(ValueError, msg=field_name):
                ProjectProfile.validate_dict({
                    "agent_analyzers": {"full_security": {field_name: 0}},
                })
