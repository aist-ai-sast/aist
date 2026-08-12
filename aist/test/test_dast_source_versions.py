from __future__ import annotations

from django.test import TestCase
from django.utils import timezone
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.integrations.dast_report import ValidatedDastReport, ValidatedDastSelection
from aist.models import (
    AISTProject,
    AISTProjectVersion,
    DastProjectBinding,
    DastTarget,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    RepositoryInfo,
    ScmType,
    VersionType,
)
from aist.services.dast_source_versions import DastSourceVersionError, resolve_dast_source_version

BACKEND_SHA = "a" * 40
FRONTEND_SHA = "b" * 40
TRIGGER_SHA = "c" * 40


def _validated_report(
    *,
    target_id: str = "cloud-app",
    source_commits: tuple[tuple[str, str], ...] = (("backend", BACKEND_SHA),),
) -> ValidatedDastReport:
    return ValidatedDastReport(
        contract_version="2.0",
        run_id="run-123",
        correlation_id="pipeline-123",
        target_id=target_id,
        status="succeeded",
        selection=ValidatedDastSelection(stand_id="qa-1", relation="exact", distance=0),
        source_commits=source_commits,
        findings_count=0,
        canonical_json=b"{}",
    )


class DastSourceVersionResolutionTests(TestCase):
    def setUp(self):
        product_type = Product_Type.objects.create(name="DAST source versions")
        self.sla = SLA_Configuration.objects.create(name="DAST source versions SLA")
        self.organization = Organization.objects.create(
            name="DAST source version organization",
            product_type=product_type,
        )
        repository = RepositoryInfo.objects.create(
            type=ScmType.GITHUB,
            repo_owner="acme",
            repo_name="not-the-provider-key",
        )
        product = Product.objects.create(
            name="DAST source version product",
            description="desc",
            prod_type=product_type,
            sla_configuration=self.sla,
        )
        self.project = AISTProject.objects.create(
            product=product,
            supported_languages=["python"],
            compilable=False,
            profile={},
            repository=repository,
        )
        self.integration = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.DAST,
            name="DAST source version integration",
            is_active=True,
        )
        self.target = self._target("cloud-app", ["backend", "frontend"])
        self.binding = DastProjectBinding.objects.create(
            project=self.project,
            target=self.target,
            source_repo_key="backend",
            enabled=True,
        )

    def _target(self, provider_id: str, repository_keys: list[str]) -> DastTarget:
        return DastTarget.objects.create(
            integration=self.integration,
            provider_id=provider_id,
            display_name=provider_id,
            contract_revision="2.0",
            capability_revision=f"sha256:{provider_id}",
            schema_digest=f"sha256:{provider_id}-schema",
            parameter_schema={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
            },
            provider_defaults={},
            repository_keys=repository_keys,
            launch_requirements=["repository-trigger"],
            autonomous_ready=True,
            last_seen_at=timezone.now(),
        )

    def _second_binding(self) -> DastProjectBinding:
        repository = RepositoryInfo.objects.create(
            type=ScmType.GITHUB,
            repo_owner="acme",
            repo_name="another-unrelated-name",
        )
        product = Product.objects.create(
            name="DAST frontend product",
            description="desc",
            prod_type=self.organization.product_type,
            sla_configuration=self.sla,
        )
        project = AISTProject.objects.create(
            product=product,
            supported_languages=["typescript"],
            compilable=False,
            profile={},
            repository=repository,
        )
        target = self._target("cloud-frontend", ["backend", "frontend"])
        return DastProjectBinding.objects.create(
            project=project,
            target=target,
            source_repo_key="frontend",
            enabled=True,
        )

    def test_exact_binding_key_creates_and_reuses_actual_git_hash_version(self):
        trigger = AISTProjectVersion.objects.create(
            project=self.project,
            version=TRIGGER_SHA,
            version_type=VersionType.GIT_HASH,
        )
        report = _validated_report(source_commits=(("frontend", FRONTEND_SHA), ("backend", BACKEND_SHA)))

        first = resolve_dast_source_version(report, self.binding)
        second = resolve_dast_source_version(report, self.binding)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.project_id, self.project.pk)
        self.assertEqual(first.version, BACKEND_SHA)
        self.assertEqual(first.version_type, VersionType.GIT_HASH)
        self.assertNotEqual(first.pk, trigger.pk)
        self.assertEqual(
            AISTProjectVersion.objects.filter(
                project=self.project,
                version=BACKEND_SHA,
                version_type=VersionType.GIT_HASH,
            ).count(),
            1,
        )

    def test_two_bindings_select_their_own_source_from_multiple_repositories(self):
        second_binding = self._second_binding()
        first_report = _validated_report(
            source_commits=(("backend", BACKEND_SHA), ("frontend", FRONTEND_SHA)),
        )
        second_report = _validated_report(
            target_id="cloud-frontend",
            source_commits=(("backend", BACKEND_SHA), ("frontend", FRONTEND_SHA)),
        )

        backend = resolve_dast_source_version(first_report, self.binding)
        frontend = resolve_dast_source_version(second_report, second_binding)

        self.assertEqual(backend.version, BACKEND_SHA)
        self.assertEqual(frontend.version, FRONTEND_SHA)
        self.assertEqual(frontend.project_id, second_binding.project_id)

    def test_missing_or_ambiguous_binding_source_fails_without_creating_version(self):
        reports = (
            (_validated_report(source_commits=(("frontend", FRONTEND_SHA),)), "does not contain"),
            (
                _validated_report(source_commits=(("backend", BACKEND_SHA), ("backend", FRONTEND_SHA))),
                "ambiguous",
            ),
        )
        for report, message in reports:
            with self.subTest(message=message), self.assertRaisesRegex(DastSourceVersionError, message):
                resolve_dast_source_version(report, self.binding)

        self.assertFalse(
            AISTProjectVersion.objects.filter(project=self.project, version_type=VersionType.GIT_HASH).exists(),
        )

    def test_report_target_must_match_binding_target(self):
        with self.assertRaisesRegex(DastSourceVersionError, "target does not match"):
            resolve_dast_source_version(_validated_report(target_id="another-target"), self.binding)

    def test_binding_project_requires_an_explicit_repository(self):
        self.project.repository = None
        self.project.save(update_fields=["repository", "updated"])
        self.project.refresh_from_db()

        with self.assertRaisesRegex(DastSourceVersionError, "no linked repository"):
            resolve_dast_source_version(_validated_report(), self.binding)

    def test_raw_mapping_is_not_accepted_as_a_validated_report(self):
        with self.assertRaisesRegex(DastSourceVersionError, "validated report"):
            resolve_dast_source_version({"source_commits": {"backend": BACKEND_SHA}}, self.binding)
