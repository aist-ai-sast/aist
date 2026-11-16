"""Migration 0059 rewrites stored launch settings that launches now refuse into what they already behaved as."""
from __future__ import annotations

from aist.profile import ProjectProfile
from aist.test.migration_state import HistoricalMigrationTestCase, seed_historical_organization, seed_historical_project

STORED_PROFILES = {
    "seeded labels": {"team": "Platform", "environment": "staging"},
    "typo beside a real section": {"path": {"exclude": ["vendor/"]}, "paths": {"exclude": ["tests/"]}},
    "bare string and junk entries": {
        "paths": {"exclude": "generated/", "glob": "*.min.js"},
        "severity": {"exclude": ["Info", "low", ""]},
        "analyzers": {"include": ["semgrep", 7], "exclude": "bearer"},
        "ai_triage": {"type": "local", "model": "x"},
        "agent_analyzers": {"full_security": {"max_files": 50, "max_bytes": 0, "max_findings": True}},
    },
    "unknown triage type": {"ai_triage": {"type": "manual"}},
    "already valid": {"paths": {"exclude": ["docs/"]}, "ai_triage": {"type": "n8n"}},
}


class AdmissibleStoredLaunchSettingsMigrationTests(HistoricalMigrationTestCase):
    migrate_from = [("aist", "0058_clear_vendor_marketing_announcement")]
    migrate_to = [("aist", "0059_admissible_stored_launch_settings")]

    def prepare(self, old_apps):
        AISTProjectLaunchConfig = old_apps.get_model("aist", "AISTProjectLaunchConfig")

        organization = seed_historical_organization(old_apps, "Stored settings")
        self.project_ids = {
            label: seed_historical_project(
                old_apps, f"Stored settings {index}", product_type=organization.product_type, profile=profile,
            ).id
            for index, (label, profile) in enumerate(STORED_PROFILES.items())
        }
        project_id = self.project_ids["already valid"]
        self.legacy_config_id = AISTProjectLaunchConfig.objects.create(
            project_id=project_id,
            name="Seeded nightly",
            execution_type="SAST",
            params={"analyzers": ["semgrep"], "time_class_level": "normal"},
        ).id
        self.fast_config_id = AISTProjectLaunchConfig.objects.create(
            project_id=project_id,
            name="Fast preset",
            execution_type="SAST",
            params={"analyzers": ["semgrep"], "time_class_level": "fast"},
        ).id

    def test_every_stored_profile_can_be_saved_again_and_reads_as_before(self):
        AISTProject = self.apps.get_model("aist", "AISTProject")

        for label, stored in STORED_PROFILES.items():
            with self.subTest(label):
                migrated = AISTProject.objects.get(pk=self.project_ids[label]).profile

                ProjectProfile.validate_dict(migrated)
                self.assertEqual(ProjectProfile.from_dict(migrated), ProjectProfile.from_dict(stored))

    def test_a_valid_profile_is_untouched(self):
        AISTProject = self.apps.get_model("aist", "AISTProject")

        self.assertEqual(
            AISTProject.objects.get(pk=self.project_ids["already valid"]).profile,
            STORED_PROFILES["already valid"],
        )

    def test_a_launch_config_with_an_unknown_time_class_runs_slow_as_before(self):
        AISTProjectLaunchConfig = self.apps.get_model("aist", "AISTProjectLaunchConfig")

        legacy = AISTProjectLaunchConfig.objects.get(pk=self.legacy_config_id).params
        fast = AISTProjectLaunchConfig.objects.get(pk=self.fast_config_id).params

        self.assertEqual(legacy, {"analyzers": ["semgrep"], "time_class_level": "slow"})
        self.assertEqual(fast["time_class_level"], "fast")
