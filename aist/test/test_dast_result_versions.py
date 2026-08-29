from __future__ import annotations

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.integrations.dast_report import ValidatedDastReport
from aist.models import (
    AISTProject,
    AISTProjectVersion,
    Organization,
    RepositoryInfo,
    ScmType,
    VersionType,
)
from aist.services.dast_result_versions import ensure_dast_target_version, resolve_dast_result_version
from aist.test import dast_fixtures

ACTUAL_SHA = "a" * 40


def _validated_report(*, target_id: str, source_commits: tuple[tuple[str, str], ...]) -> ValidatedDastReport:
    return ValidatedDastReport(
        run_id="run-1",
        target_id=target_id,
        source_commits=source_commits,
        findings_count=0,
        canonical_json=b"{}",
    )


class DastResultVersionTests(TestCase):

    """Every finalized DAST report resolves to exactly one project version, both target shapes."""

    def setUp(self):
        product_type = Product_Type.objects.create(name="DAST result versions")
        sla = SLA_Configuration.objects.create(name="DAST result versions SLA")
        self.organization = Organization.objects.create(
            name="DAST result version organization",
            product_type=product_type,
        )
        product = Product.objects.create(
            name="DAST result version product",
            description="desc",
            prod_type=product_type,
            sla_configuration=sla,
        )
        self.project = AISTProject.objects.create(
            product=product,
            supported_languages=["python"],
            compilable=False,
            profile={},
            repository=RepositoryInfo.objects.create(
                type=ScmType.GITHUB,
                repo_owner="acme",
                repo_name="backend",
            ),
        )
        self.integration, _state = dast_fixtures.create_dast_integration(
            organization=self.organization,
            public_id="result-versions",
        )

    def _binding(self, shape, provider_id, **wire_overrides):
        target = dast_fixtures.create_dast_target(
            integration=self.integration,
            wire=shape.wire(provider_id, **wire_overrides),
            seen_at=timezone.now(),
        )
        return dast_fixtures.create_dast_binding(project=self.project, target=target, parameters={})

    def test_source_based_result_resolves_to_the_reported_commit(self):
        binding = self._binding(dast_fixtures.SOURCE_BASED, "cloud-app")
        report = _validated_report(target_id="cloud-app", source_commits=(("cloud-app", ACTUAL_SHA),))

        version = resolve_dast_result_version(report, binding)

        self.assertEqual(version.version, ACTUAL_SHA)
        self.assertEqual(version.version_type, VersionType.GIT_HASH)

    def test_perimeter_result_resolves_to_one_reusable_version_per_target(self):
        binding = self._binding(dast_fixtures.PERIMETER, "edge-perimeter")
        report = _validated_report(target_id="edge-perimeter", source_commits=())

        first = resolve_dast_result_version(report, binding)
        second = resolve_dast_result_version(report, binding)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.project_id, self.project.pk)
        self.assertEqual(first.version, "edge-perimeter")
        self.assertEqual(first.version_type, VersionType.DAST_TARGET)
        self.assertEqual(AISTProjectVersion.objects.filter(version_type=VersionType.DAST_TARGET).count(), 1)

    def test_two_perimeter_targets_in_one_project_keep_separate_versions(self):
        first_binding = self._binding(dast_fixtures.PERIMETER, "edge-perimeter")
        second_binding = self._binding(dast_fixtures.PERIMETER, "partner-perimeter")

        first = ensure_dast_target_version(first_binding)
        second = ensure_dast_target_version(second_binding)

        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual({first.version, second.version}, {"edge-perimeter", "partner-perimeter"})

    def test_a_long_provider_id_survives_as_the_version_value(self):
        # The catalog allows a 255-character provider id, and the version column has to hold it:
        # truncating here would show the operator a clipped target name in the findings list.
        provider_id = "p" * 255
        binding = self._binding(dast_fixtures.PERIMETER, provider_id, display_name="Long perimeter target")

        version = ensure_dast_target_version(binding)

        self.assertEqual(version.version, provider_id)

    def test_a_target_version_rejects_an_archive_and_a_branch_parent(self):
        binding = self._binding(dast_fixtures.PERIMETER, "edge-perimeter")
        version = ensure_dast_target_version(binding)
        branch = AISTProjectVersion.objects.create(
            project=self.project,
            version="main",
            version_type=VersionType.GIT_BRANCH,
        )

        version.resolved_from_branch = branch
        with self.assertRaises(ValidationError):
            version.full_clean(exclude=["id"], validate_unique=False, validate_constraints=False)

        version.resolved_from_branch = None
        version.version = ""
        with self.assertRaises(ValidationError):
            version.full_clean(exclude=["id"], validate_unique=False, validate_constraints=False)
