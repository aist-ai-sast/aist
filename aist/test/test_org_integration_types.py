"""
Model-level tests for ``OrgIntegrationType`` enum and CLAUDE_CODE
single-active constraint introduced in Task 1 of the Claude-as-Integration
refactor (``docs/plans/2026-05-12-claude-as-org-integration.md``).
"""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.test import TestCase
from dojo.models import Product_Type

from aist.models import Organization, OrgIntegration, OrgIntegrationType, ScmType


class OrgIntegrationTypeEnumTests(TestCase):

    def test_claude_code_choice_present(self):
        self.assertEqual(OrgIntegrationType.CLAUDE_CODE.value, "CLAUDE_CODE")
        labels = dict(OrgIntegrationType.choices)
        self.assertIn("CLAUDE_CODE", labels)
        self.assertEqual(labels["CLAUDE_CODE"], "Claude Code")

    def test_gerrit_choices_present(self):
        self.assertEqual(OrgIntegrationType.GERRIT.value, "GERRIT")
        self.assertIn("GERRIT", dict(OrgIntegrationType.choices))
        self.assertEqual(ScmType.GERRIT.value, "GERRIT")
        self.assertIn("GERRIT", dict(ScmType.choices))


class OrgIntegrationSingleActiveClaudeConstraintTests(TestCase):

    """
    Partial UniqueConstraint: only one active CLAUDE_CODE per org.

    The constraint must NOT affect other integration types — GitHub/GitLab
    legitimately support multiple active integrations per org (different
    repo bindings each get their own PAT). This is the exact reason for
    making the constraint *partial* (scoped to ``integration_type='CLAUDE_CODE'``).
    """

    def setUp(self):
        self.org = Organization.objects.create(
            name="Constraint Test Org",
            product_type=Product_Type.objects.create(name="Constraint PT"),
        )

    def _create_claude(self, *, name: str, is_active: bool) -> OrgIntegration:
        return OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.CLAUDE_CODE,
            name=name,
            secret="sk-ant-oat01-" + "x" * 30,
            is_active=is_active,
        )

    def test_only_one_active_claude_integration_per_org(self):
        self._create_claude(name="primary", is_active=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._create_claude(name="secondary", is_active=True)

    def test_inactive_claude_does_not_block_new_active(self):
        self._create_claude(name="legacy", is_active=False)
        # New active row alongside an inactive one — must succeed; the
        # inactive row stays in DB for audit/rollback.
        new_active = self._create_claude(name="current", is_active=True)
        self.assertTrue(new_active.is_active)
        self.assertEqual(
            OrgIntegration.objects.filter(
                organization=self.org,
                integration_type=OrgIntegrationType.CLAUDE_CODE,
            ).count(),
            2,
        )

    def test_constraint_does_not_affect_other_types(self):
        # Two active GITHUB integrations on the same org — must remain allowed.
        OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.GITHUB,
            name="repo-a",
            secret="ghp_aaa",  # noqa: S106 -- test fixture
            is_active=True,
        )
        OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.GITHUB,
            name="repo-b",
            secret="ghp_bbb",  # noqa: S106 -- test fixture
            is_active=True,
        )
        self.assertEqual(
            OrgIntegration.objects.filter(
                organization=self.org,
                integration_type=OrgIntegrationType.GITHUB,
                is_active=True,
            ).count(),
            2,
        )
