"""
Lock the structural contract of the aist-full-security-review SKILL.md.

The skill drives Claude when the SAST pipeline invokes the bridge for the
``claude-full-security`` analyzer. Future edits must not silently drop the
parts the rest of the system depends on:

- the manifest-first methodology that distinguishes full-scan from diff-scan,
- the runtime-sidecar keys that ``build_agent_runtime_env`` provides,
- the TP-only emission policy + uncertaintyLevel-based confidence,
- the 1:1 invariant between ``unique_id_from_tool`` and ``uniqueIdFromTool``,
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
    / "aist-full-security-review"
    / "SKILL.md"
)


class FullSecurityReviewSkillContractTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = _SKILL_PATH.read_text(encoding="utf-8")
        cls.lower = cls.text.lower()

    def test_skill_file_exists(self):
        self.assertTrue(_SKILL_PATH.exists(), _SKILL_PATH)

    # ── Section headings ────────────────────────────────────────────────

    def test_required_top_level_headings_present(self):
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

    # ── Manifest-first methodology ──────────────────────────────────────

    def test_manifest_first_methodology_locked(self):
        # The full-scan skill must NOT dump every file into context. Manifest
        # building is the central tool for keeping the prompt bounded.
        self.assertRegex(
            self.text,
            r"\bmanifest\b",
            "skill must reference a manifest-first methodology",
        )

    def test_sub_project_manifest_heuristic_documented(self):
        # Reuse the sub-project markers list — mirrors diff skill so callers
        # working both analyzers see consistent guidance.
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
        self.assertIn("true_positive", self.text)
        self.assertIn("uncertaintyLevel", self.text)
        fp_empty_pattern = re.compile(r"false_positives\[\][^.]{0,120}\bempty\b", re.IGNORECASE | re.DOTALL)
        self.assertRegex(
            self.text,
            fp_empty_pattern,
            "skill must document that false_positives[] stays empty",
        )

    def test_uncertainty_level_thresholds_documented(self):
        self.assertRegex(self.text, r"uncertaintyLevel\s*[≤<=]\s*0\.2", "low-uncertainty threshold (≤0.2) missing")
        self.assertRegex(
            self.text,
            r"uncertaintyLevel\s*∈\s*\[0\.4,\s*0\.7\]|0\.4\D{1,12}0\.7",
            "high-uncertainty range [0.4, 0.7] missing",
        )

    # ── 1:1 invariant ───────────────────────────────────────────────────

    def test_1_to_1_invariant_documented(self):
        self.assertIn("uniqueIdFromTool", self.text)
        self.assertIn("unique_id_from_tool", self.text)
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

    # ── No diff baseline ─────────────────────────────────────────────────

    def test_does_not_consume_base_commit(self):
        # Full-scan reasons over the deployable revision as a whole. Mentioning
        # BASE_COMMIT here would create a contract mismatch with the YAML env
        # list (Task 4) and the runtime sidecar (Task 3).
        self.assertNotIn(
            "BASE_COMMIT",
            self.text,
            "full skill must not consume BASE_COMMIT — that's the diff skill's contract",
        )

    # ── Runtime sidecar keys ────────────────────────────────────────────

    def test_runtime_sidecar_input_documented(self):
        self.assertIn("runtime_filename", self.text)
        self.assertIn("EXCLUDED_PATHS_JSON", self.text)
        for key in (
            "AGENT_FULL_MAX_FILES",
            "AGENT_FULL_MAX_BYTES",
            "AGENT_FULL_MAX_FILE_BYTES",
            "AGENT_FULL_MAX_FINDINGS",
        ):
            self.assertIn(key, self.text, f"runtime sidecar key missing: {key!r}")

    def test_exclusion_pattern_semantics_documented(self):
        for phrase in (
            "same simple rule as AIST post-processing",
            "exclusion string is contained",
            "cloud/tests/foo.py",
            ".spec.ts",
        ):
            self.assertIn(phrase, self.text)

    # ── Output schema fidelity ──────────────────────────────────────────

    def test_severity_enum_listed_with_exact_case(self):
        for sev in ("Critical", "High", "Medium", "Low", "Info"):
            self.assertIn(sev, self.text, f"severity value missing: {sev!r}")

    def test_fix_must_be_null_for_false_positive(self):
        pattern = re.compile(r"`?fix`?\s+MUST\s+be\s+`?null`?", re.IGNORECASE)
        self.assertRegex(self.text, pattern, "schema rule about fix=null for FP missing")

    def test_output_filenames_are_full_security(self):
        # The truncation marker must reflect the analyzer name in YAML so the
        # bridge picks it up. result_filename / ai_response_filename come from
        # the bridge args; the skill should reference them by token, not by
        # hardcoded name (apart from the truncation flag).
        self.assertIn("claude-full-security_truncated.flag", self.text)

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

    def test_truncation_policy_escalates_pipeline_status(self):
        self.assertRegex(
            self.text,
            r"FINISHED_WITH_WARNINGS",
            "truncation must escalate pipeline status to FINISHED_WITH_WARNINGS",
        )

    def test_max_findings_cap_documented(self):
        # The full-scan skill caps output to keep the analyst review bounded;
        # AGENT_FULL_MAX_FINDINGS is the value driving that cap.
        cap_pattern = re.compile(
            r"AGENT_FULL_MAX_FINDINGS[^\n]{0,200}(cap|limit|maximum|max)",
            re.IGNORECASE | re.DOTALL,
        )
        self.assertRegex(self.text, cap_pattern, "AGENT_FULL_MAX_FINDINGS cap not documented")

    # ── Framework-agnostic phrasing ─────────────────────────────────────

    def test_no_scanner_or_tool_vendor_names(self):
        for name in ("Semgrep", "CodeQL", "Snyk", "SonarQube", "Bearer", "Infer"):
            self.assertNotIn(
                name,
                self.text,
                f"skill body must not name scanner/vendor {name!r}",
            )

    def test_no_framework_specific_api_names(self):
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

    def test_full_scope_locking(self):
        # Counterpart to the diff skill's "introduced by the diff" anchor.
        # We require the skill to declare a deployable-code, present-vulnerability
        # scope so Claude doesn't drift back into diff-style reasoning.
        self.assertRegex(
            self.text,
            r"deployable\s+(code|revision|project)",
            "skill must lock scope to deployable code present at the scanned revision",
        )

    def test_high_confidence_tp_only_anchor(self):
        # The full skill is even more sensitive to FP flooding than diff —
        # explicitly require a high-confidence / TP-only emission anchor.
        self.assertRegex(
            self.text,
            r"high[-\s]?confidence",
            "skill must lock to a high-confidence emission policy",
        )


if __name__ == "__main__":
    unittest.main()
