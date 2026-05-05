from __future__ import annotations

from django.conf import settings

from aist.test.test_api import AISTApiBase
from aist.utils.diff_baseline import _positive_int_setting

FULL_SCAN_SETTINGS: tuple[str, ...] = (
    "AGENT_FULL_MAX_FILES",
    "AGENT_FULL_MAX_BYTES",
    "AGENT_FULL_MAX_FILE_BYTES",
    "AGENT_FULL_MAX_FINDINGS",
)


class FullSecuritySettingsTests(AISTApiBase):

    """
    Locks the AGENT_FULL_MAX_* defaults so the runtime-env builder (Task 3)
    has stable Django-side defaults to fall back on when a project profile
    does not override them.
    """

    def test_all_settings_exist(self):
        for name in FULL_SCAN_SETTINGS:
            self.assertTrue(
                hasattr(settings, name),
                msg=f"missing Django setting: {name}",
            )

    def test_all_defaults_are_positive_integers(self):
        for name in FULL_SCAN_SETTINGS:
            value = _positive_int_setting(name)
            self.assertIsInstance(value, int)
            self.assertGreater(value, 0, msg=f"{name} must be > 0")

    def test_default_values_are_conservative(self):
        # The plan's documented defaults — sized to fit enterprise repos but
        # not unbounded. These also drive the docker-compose env pass-through.
        # Pin via override_settings so the test is independent of env vars
        # the runtime container may set on top of settings.py defaults.
        with self.settings(
            AGENT_FULL_MAX_FILES=1500,
            AGENT_FULL_MAX_BYTES=8000000,
            AGENT_FULL_MAX_FILE_BYTES=200000,
            AGENT_FULL_MAX_FINDINGS=50,
        ):
            self.assertEqual(_positive_int_setting("AGENT_FULL_MAX_FILES"), 1500)
            self.assertEqual(_positive_int_setting("AGENT_FULL_MAX_BYTES"), 8000000)
            self.assertEqual(_positive_int_setting("AGENT_FULL_MAX_FILE_BYTES"), 200000)
            self.assertEqual(_positive_int_setting("AGENT_FULL_MAX_FINDINGS"), 50)

    def test_zero_or_negative_setting_is_rejected(self):
        for name in FULL_SCAN_SETTINGS:
            for bad in (0, -1):
                with self.settings(**{name: bad}), self.assertRaises(ValueError, msg=f"{name}={bad}"):
                    _positive_int_setting(name)

    def test_non_integer_setting_is_rejected(self):
        for name in FULL_SCAN_SETTINGS:
            with self.settings(**{name: "not-a-number"}), self.assertRaises(ValueError, msg=name):
                _positive_int_setting(name)
