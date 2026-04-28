"""
Lock the structural contract of the aist-diff-security-review SKILL.md.

The skill is the prompt that drives Claude when the SAST pipeline invokes
the bridge for the claude-diff-security analyzer. Future edits must not
silently drop the parts that the rest of the system depends on:

- the section structure that organizes the methodology,
- the runtime-sidecar / output-path interface contract,
- the TP-only emission policy + uncertaintyLevel-based confidence,
- the 1:1 invariant between `unique_id_from_tool` and `uniqueIdFromTool`,
- the framework-agnostic phrasing (no library API names, no scanner names).
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

_SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / ".codex"
    / "skills"
    / "aist-diff-security-review"
    / "SKILL.md"
)


class DiffSecurityReviewSkillContractTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = _SKILL_PATH.read_text(encoding="utf-8")
        cls.lower = cls.text.lower()

    def test_skill_file_exists(self):
        self.assertTrue(_SKILL_PATH.exists(), _SKILL_PATH)

    # ── Section headings ────────────────────────────────────────────────

    def test_required_top_level_headings_present(self):
        # Match heading text without level so future edits can re-nest the
        # document without breaking the contract.
        for label in (
            "Role and objective",
            "Scope",
            "Hard exclusions",
            "Inputs",
            "Methodology",
            "Vulnerability classes",
            "Triage decision rules",
            "Output",
            "Self-check",
        ):
            pattern = re.compile(rf"^#+\s+.*\b{re.escape(label)}\b.*$", re.MULTILINE | re.IGNORECASE)
            self.assertRegex(self.text, pattern, f"missing heading: {label!r}")

    def test_three_phase_methodology_headings(self):
        for phase in ("Phase 1", "Phase 2", "Phase 3"):
            pattern = re.compile(rf"^#+\s+{re.escape(phase)}\b.*", re.MULTILINE)
            self.assertRegex(self.text, pattern, f"missing methodology phase: {phase!r}")

    # ── Sub-project detection ───────────────────────────────────────────

    def test_sub_project_manifest_heuristic_documented(self):
        # The user explicitly asked for sub-project awareness — locking that
        # the manifest list survives future edits.
        for manifest in (
            "package.json",
            "pyproject.toml",
            "go.mod",
            "Cargo.toml",
            "pom.xml",
        ):
            self.assertIn(
                manifest,
                self.text,
                f"sub-project manifest marker missing: {manifest!r}",
            )

    # ── Triage / TP-only policy ─────────────────────────────────────────

    def test_triage_documents_tp_only_emission(self):
        # Both TP and uncertaintyLevel must appear; the FP bucket must be
        # documented as staying empty in normal operation.
        self.assertIn("true_positive", self.text)
        self.assertIn("uncertaintyLevel", self.text)
        # Loose match on "false_positives[] ... empty" so future copy-edits
        # don't break this guard.
        fp_empty_pattern = re.compile(r"false_positives\[\][^.]{0,120}\bempty\b", re.IGNORECASE | re.DOTALL)
        self.assertRegex(
            self.text,
            fp_empty_pattern,
            "skill must document that false_positives[] stays empty",
        )

    def test_uncertainty_level_thresholds_documented(self):
        # Low-uncertainty (confident) and high-uncertainty (likely) tiers
        # are pillars of the TP-only policy.
        self.assertRegex(self.text, r"uncertaintyLevel\s*[≤<=]\s*0\.2", "low-uncertainty threshold (≤0.2) missing")
        self.assertRegex(
            self.text,
            r"uncertaintyLevel\s*∈\s*\[0\.4,\s*0\.7\]|0\.4\D{1,12}0\.7",
            "high-uncertainty range [0.4, 0.7] missing",
        )

    # ── 1:1 invariant ───────────────────────────────────────────────────

    def test_1_to_1_invariant_documented(self):
        # uniqueIdFromTool ↔ unique_id_from_tool join must be explicitly
        # stated so the post-import sync's drop-orphans behavior matches.
        self.assertIn("uniqueIdFromTool", self.text)
        self.assertIn("unique_id_from_tool", self.text)
        # And there's a sentence asserting the match.
        match_pattern = re.compile(
            r"uniqueIdFromTool[^\n]{0,200}match[^\n]{0,200}unique_id_from_tool"
            r"|unique_id_from_tool[^\n]{0,200}match[^\n]{0,200}uniqueIdFromTool",
            re.IGNORECASE | re.DOTALL,
        )
        self.assertRegex(
            self.text,
            match_pattern,
            "1:1 invariant prose between uniqueIdFromTool and unique_id_from_tool missing",
        )

    # ── BASE fallback chain ─────────────────────────────────────────────

    def test_three_level_base_fallback_documented(self):
        self.assertIn("BASE_COMMIT", self.text)
        self.assertIn("git log --since='14 days ago'", self.text)
        self.assertIn("git rev-list --max-parents=0 HEAD", self.text)

    # ── Output schema fidelity ──────────────────────────────────────────

    def test_severity_enum_listed_with_exact_case(self):
        for sev in ("Critical", "High", "Medium", "Low", "Info"):
            self.assertIn(sev, self.text, f"severity value missing: {sev!r}")

    def test_fix_must_be_null_for_false_positive(self):
        # We don't normally emit FP, but the schema constraint must remain
        # documented so the fidelity check holds if the verdict ever flips.
        pattern = re.compile(r"`?fix`?\s+MUST\s+be\s+`?null`?", re.IGNORECASE)
        self.assertRegex(self.text, pattern, "schema rule about fix=null for FP missing")

    def test_truncation_policy_documented(self):
        self.assertIn("claude-diff-security_truncated.flag", self.text)
        self.assertRegex(
            self.text,
            r"FINISHED_WITH_WARNINGS",
            "truncation must escalate pipeline status to FINISHED_WITH_WARNINGS",
        )

    def test_file_path_is_relative_to_source_path(self):
        self.assertRegex(
            self.text,
            r"file_path.*relative to `source_path`",
            "result file paths must be relative to source_path",
        )
        self.assertIn(
            "Never prefix it with the basename of `source_path`",
            self.text,
        )
        self.assertIn(
            "Path(source_path) / file_path",
            self.text,
        )

    def test_runtime_sidecar_input_documented(self):
        # The sidecar JSON file is the only path through which BASE_COMMIT,
        # excluded paths, and limits reach the skill — env vars don't survive
        # the cross-container boundary into the bridge container.
        self.assertIn("runtime_filename", self.text)
        self.assertIn("EXCLUDED_PATHS_JSON", self.text)
        self.assertIn("CLAUDE_DIFF_MAX_FILES", self.text)
        self.assertIn("CLAUDE_DIFF_MAX_BYTES", self.text)

    def test_exclusion_pattern_semantics_documented(self):
        for phrase in (
            "same simple rule as AIST post-processing",
            "exclusion string is contained",
            "cloud/tests/foo.py",
            ".spec.ts",
        ):
            self.assertIn(phrase, self.text)

    # ── Framework-agnostic phrasing ─────────────────────────────────────

    def test_no_scanner_or_tool_vendor_names(self):
        # The skill itself instructs Claude to avoid scanner names; the
        # skill body must not embed any well-known scanner names that
        # would set a contradictory example.
        for name in ("Semgrep", "CodeQL", "Snyk", "SonarQube", "Bearer", "Infer"):
            self.assertNotIn(
                name,
                self.text,
                f"skill body must not name scanner/vendor {name!r}",
            )

    def test_no_framework_specific_api_names(self):
        # The user explicitly forbade ecosystem-specific examples like
        # `requests.get`, `os.path.join`, `urllib`. The skill must describe
        # vulnerabilities by behavior, not by symbol.
        forbidden_apis = (
            r"\brequests\.\w+\(",
            r"\bos\.path\.join\(",
            r"\burllib\b",
            r"\bsubprocess\.\w+\(",
            r"\beval\(",
            r"\bpickle\.\w+\(",
            r"\byaml\.load\(",
            r"\bexec\(",
        )
        for pat in forbidden_apis:
            with self.subTest(pattern=pat):
                self.assertNotRegex(
                    self.text,
                    re.compile(pat),
                    f"skill body must not name framework API: {pat!r}",
                )

    # ── Persona & scope locking ─────────────────────────────────────────

    def test_persona_is_security_engineer(self):
        self.assertRegex(
            self.text,
            r"\bsecurity\s+engineer\b",
            "skill must adopt a security-engineer persona",
        )

    def test_regressions_only_scope_locking(self):
        # The skill must reject general code review and stick to "introduced
        # by the diff" findings — this is the single biggest FP-reducer.
        self.assertRegex(
            self.text,
            r"introduced\s+by\s+the\s+diff",
            "skill must lock scope to regressions introduced by the diff",
        )

    def test_high_risk_auth_refactor_class_called_out(self):
        # ANAS-182-style class: state-transition regressions during
        # auth/onboarding refactors. The class must be explicitly named so
        # Claude prioritizes hunks in those files.
        for keyword in (
            "registration",
            "activation",
            "password-reset",
            "state-transition",
        ):
            self.assertIn(
                keyword,
                self.lower,
                f"high-risk auth-refactor class missing keyword: {keyword!r}",
            )


if __name__ == "__main__":
    unittest.main()
