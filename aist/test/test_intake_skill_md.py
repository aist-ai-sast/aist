"""
Lock the structural contract of the intake-review SKILL.md files.

These two skills drive Claude when the SAST pipeline invokes the bridge for
the ``claude-intake-review`` (whole-revision) and ``claude-intake-diff``
(diff) analyzers — supply-chain vetting of untrusted third-party source.
Future edits must not silently drop the parts the rest of the system
depends on:

- the inverted threat model (author is a potential adversary),
- the two-output-class model (confirmed-malicious + review-required),
- the runtime-sidecar keys that ``build_agent_runtime_env`` provides
  (full-budget keys for intake-review, diff-baseline keys for intake-diff),
- the 1:1 invariant between ``unique_id_from_tool`` and ``uniqueIdFromTool``,
- the ``source_path`` argument the bridge interpolates (NOT
  ``target_repo_path`` — that would change the prompt arg name),
- the core intake indicator families (undeclared URLs, obfuscated blobs,
  install hooks, backdoor triggers).
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

_SKILLS_ROOT = Path(__file__).resolve().parents[2] / ".codex" / "skills"
_FULL_PATH = _SKILLS_ROOT / "aist-intake-review" / "SKILL.md"
_DIFF_PATH = _SKILLS_ROOT / "aist-intake-diff-review" / "SKILL.md"


class _IntakeSkillContractMixin:
    PATH: Path

    @classmethod
    def setUpClass(cls):
        cls.text = cls.PATH.read_text(encoding="utf-8")
        cls.lower = cls.text.lower()

    def test_skill_file_exists(self):
        self.assertTrue(self.PATH.exists(), self.PATH)

    def test_inverted_threat_model_stated(self):
        # The whole point: the author of the reviewed code is the adversary.
        self.assertIn("adversary", self.lower)
        self.assertRegex(self.lower, r"third-party|supply-chain|supply chain")

    def test_two_output_classes_documented(self):
        # Confirmed-malicious AND the lower-confidence review-required bar.
        self.assertIn("review required", self.lower)
        self.assertRegex(self.lower, r"confirmed[ -]malicious|confirmed malicious")

    def test_review_required_title_prefix_locked(self):
        # The pipeline/humans key off this exact prefix to triage indicators.
        self.assertIn("Review required:", self.text)

    def test_core_indicator_families_present(self):
        for needle in (
            "exfiltration",
            "obfuscat",
            "install",
            "backdoor",
            "embedded",
        ):
            self.assertIn(needle, self.lower, f"missing indicator family: {needle!r}")

    def test_not_an_indicator_section_present(self):
        # Guards the camera/device-API false-alarm class: example URLs in
        # comments/docs and the component's own declared backend must not
        # trigger findings.
        self.assertRegex(self.text, re.compile(r"^#+\s+What is NOT an indicator", re.MULTILINE))
        self.assertIn("comment", self.lower)
        self.assertRegex(self.lower, r"never passed to a network|live destination of an outbound")
        self.assertRegex(self.lower, r"declared (api|endpoint|set|backend)")

    def test_uses_source_path_argument(self):
        # The bridge only rewrites to target_repo_path when the skill text
        # contains that token. Intake skills must use source_path.
        self.assertIn("source_path", self.text)
        self.assertNotIn("target_repo_path", self.text)

    def test_unique_id_one_to_one_invariant(self):
        self.assertIn("unique_id_from_tool", self.text)
        self.assertIn("uniqueIdFromTool", self.text)

    def test_no_scanner_vendor_name_leak_rule(self):
        self.assertRegex(self.lower, r"never name a scanner")

    def test_reasoning_section_headers_locked(self):
        for header in ("## Verdict", "## Evidence", "## Reproduction", "## Impact", "## Remediation"):
            self.assertIn(header, self.text, f"missing reasoning header: {header!r}")

    def test_required_top_level_headings_present(self):
        for label in ("Role and objective", "Inputs", "Output", "Self-check"):
            pattern = re.compile(rf"^#+\s+.*\b{re.escape(label)}\b.*$", re.MULTILINE | re.IGNORECASE)
            self.assertRegex(self.text, pattern, f"missing heading: {label!r}")


class IntakeFullSkillContractTests(_IntakeSkillContractMixin, unittest.TestCase):
    PATH = _FULL_PATH

    def test_uses_full_budget_runtime_keys(self):
        for key in (
            "AGENT_FULL_MAX_FILES",
            "AGENT_FULL_MAX_BYTES",
            "AGENT_FULL_MAX_FILE_BYTES",
            "AGENT_FULL_MAX_FINDINGS",
            "EXCLUDED_PATHS_JSON",
        ):
            self.assertIn(key, self.text, f"missing runtime key: {key!r}")

    def test_does_not_reference_diff_baseline(self):
        # Whole-revision scan — no BASE_COMMIT baseline.
        self.assertNotIn("BASE_COMMIT", self.text)

    def test_truncation_marker_name_matches_analyzer(self):
        self.assertIn("claude-intake-review_truncated.flag", self.text)


class IntakeDiffSkillContractTests(_IntakeSkillContractMixin, unittest.TestCase):
    PATH = _DIFF_PATH

    def test_uses_diff_baseline_runtime_keys(self):
        for key in (
            "BASE_COMMIT",
            "CLAUDE_DIFF_MAX_FILES",
            "CLAUDE_DIFF_MAX_BYTES",
            "EXCLUDED_PATHS_JSON",
        ):
            self.assertIn(key, self.text, f"missing runtime key: {key!r}")

    def test_base_fallback_chain_documented(self):
        # The L1/L2/L3 BASE resolution must survive edits — it is what keeps
        # the diff scan robust against force-pushed history.
        self.assertRegex(self.text, r"\bL1\b")
        self.assertRegex(self.text, r"\bL3\b")
        self.assertIn("max-parents=0", self.text)

    def test_scope_is_diff_introduced_only(self):
        self.assertRegex(self.lower, r"introduced by the diff|diff-introduced")

    def test_truncation_marker_name_matches_analyzer(self):
        self.assertIn("claude-intake-diff_truncated.flag", self.text)


if __name__ == "__main__":
    unittest.main()
