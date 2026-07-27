from aist.execution.dast_trigger import DastTrigger, DastTriggerError
from aist.models import AISTProjectVersion, VersionType
from aist.test.test_api import AISTApiBase


class DastTriggerTests(AISTApiBase):
    def _version(self, version_type, version, **extra):
        return AISTProjectVersion.objects.create(
            project=self.project,
            version_type=version_type,
            version=version,
            **extra,
        )

    def test_branch_preserves_original_ref_and_ignores_stale_resolution(self):
        branch = self._version(
            VersionType.GIT_BRANCH,
            "release/2026.07",
            last_resolved_commit="f" * 40,
        )

        trigger = DastTrigger.from_project_version(branch, repository_key="backend")

        self.assertEqual(trigger.project_version_id, branch.pk)
        self.assertEqual(trigger.to_wire(), {
            "repository_key": "backend",
            "type": "GIT_BRANCH",
            "ref": "release/2026.07",
        })

    def test_hash_requires_and_preserves_full_commit(self):
        commit = self._version(VersionType.GIT_HASH, "a" * 40)

        trigger = DastTrigger.from_project_version(commit, repository_key="backend")

        self.assertEqual(trigger.to_wire()["ref"], "a" * 40)
        invalid = self._version(VersionType.GIT_HASH, "abc")
        with self.assertRaisesRegex(DastTriggerError, "full 40-hex"):
            DastTrigger.from_project_version(invalid, repository_key="backend")
        uppercase = self._version(VersionType.GIT_HASH, "B" * 40)
        with self.assertRaisesRegex(DastTriggerError, "full 40-hex"):
            DastTrigger.from_project_version(uppercase, repository_key="backend")

    def test_file_hash_and_untrusted_repository_key_are_rejected(self):
        file_hash = self._version(VersionType.FILE_HASH, "a" * 64)

        with self.assertRaisesRegex(DastTriggerError, "does not support"):
            DastTrigger.from_project_version(file_hash, repository_key="backend")
        with self.assertRaisesRegex(DastTriggerError, "repository key"):
            DastTrigger.from_project_version(self.pv, repository_key=" ")

    def test_unsafe_branch_is_rejected_with_provider_contract_rules(self):
        branch = self._version(VersionType.GIT_BRANCH, "../release")

        with self.assertRaisesRegex(DastTriggerError, "branch ref"):
            DastTrigger.from_project_version(branch, repository_key="backend")
