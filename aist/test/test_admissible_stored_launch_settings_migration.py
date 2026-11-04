"""Migration 0059 rewrites stored launch settings that launches now refuse into what they already behaved as."""
from __future__ import annotations

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from aist.profile import ProjectProfile
from aist.test.migration_state import historical_state

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


class AdmissibleStoredLaunchSettingsMigrationTests(TransactionTestCase):
    migrate_from = [("aist", "0058_clear_vendor_marketing_announcement")]
    migrate_to = [("aist", "0059_admissible_stored_launch_settings")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        pinned = historical_state(executor, self.migrate_from)
        executor.migrate(pinned)
        old_apps = executor.loader.project_state(pinned).apps

        ProductType = old_apps.get_model("dojo", "Product_Type")
        Product = old_apps.get_model("dojo", "Product")
        SLAConfiguration = old_apps.get_model("dojo", "SLA_Configuration")
        Organization = old_apps.get_model("aist", "Organization")
        AISTProject = old_apps.get_model("aist", "AISTProject")
        AISTProjectLaunchConfig = old_apps.get_model("aist", "AISTProjectLaunchConfig")

        product_type = ProductType.objects.create(name="Stored settings PT")
        Organization.objects.create(name="Stored settings org", product_type=product_type)
        sla = SLAConfiguration.objects.create(name="Stored settings SLA")
        self.project_ids = {}
        for index, (label, profile) in enumerate(STORED_PROFILES.items()):
            product = Product.objects.create(
                name=f"Stored settings product {index}",
                description="",
                prod_type=product_type,
                sla_configuration=sla,
            )
            self.project_ids[label] = AISTProject.objects.create(product=product, profile=profile).id
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

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

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
