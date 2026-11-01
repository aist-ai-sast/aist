"""
The role ladder in ``aist/roles.py``.

Pins two things: it answers exactly what the vendor answers while the vendor still
resolves roles, and it matches the tiers published in
``docs/security/access-control-and-roles.md``.
"""
from __future__ import annotations

from django.test import SimpleTestCase
from dojo.authorization.authorization import get_roles_for_permission
from dojo.authorization.roles_permissions import Permissions, Roles

from aist.authz.policy import ACTION_PERMISSIONS, Action
from aist.roles import (
    MINIMUM_ROLE_FOR_PERMISSION,
    ROLE_RANK,
    UndeclaredPermissionError,
    roles_for_permission,
)


class VendorEquivalenceTests(SimpleTestCase):

    """The deployment's behaviour must not move on the day we stop asking the vendor."""

    def test_every_permission_resolves_exactly_as_defectdojo_resolves_it_today(self):
        vendor_still_answers = any(
            get_roles_for_permission(permission)
            for permission in MINIMUM_ROLE_FOR_PERMISSION
        )
        if not vendor_still_answers:
            self.skipTest(
                "DefectDojo no longer resolves roles (3.x releases RBAC to its paid "
                "plugin); there is nothing left to compare against.",
            )
        for permission in MINIMUM_ROLE_FOR_PERMISSION:
            with self.subTest(permission=permission):
                self.assertEqual(
                    set(roles_for_permission(permission)),
                    set(get_roles_for_permission(permission)),
                )


class LadderCoverageTests(SimpleTestCase):
    def test_every_action_the_api_can_declare_has_a_role_requirement(self):
        """A missing row would raise at request time, inside tenant scoping."""
        for action in Action:
            with self.subTest(action=action):
                self.assertIn(ACTION_PERMISSIONS[action], MINIMUM_ROLE_FOR_PERMISSION)

    def test_an_undeclared_permission_raises_instead_of_denying_everyone(self):
        """An empty set reads as a valid "nobody qualifies", so a typo must not return one."""
        with self.assertRaises(UndeclaredPermissionError):
            roles_for_permission(Permissions.Benchmark_Edit)

    def test_a_role_satisfying_a_permission_also_satisfies_every_lower_one(self):
        for permission in MINIMUM_ROLE_FOR_PERMISSION:
            with self.subTest(permission=permission):
                qualifying = roles_for_permission(permission)
                floor = min(ROLE_RANK[role_id] for role_id in qualifying)
                self.assertEqual(
                    qualifying,
                    frozenset(r for r, rank in ROLE_RANK.items() if rank >= floor),
                )


class DocumentedTierTests(SimpleTestCase):

    """The tiers published in docs/security/access-control-and-roles.md, asserted."""

    def _roles_for(self, action: Action) -> frozenset[int]:
        return roles_for_permission(ACTION_PERMISSIONS[action])

    def test_reads_are_open_to_a_reader(self):
        for action in (
            Action.PRODUCT_READ,
            Action.FINDING_READ,
            Action.TEST_READ,
            Action.ENGAGEMENT_READ,
            Action.ORG_READ,
        ):
            with self.subTest(action=action):
                self.assertIn(Roles.Reader.value, self._roles_for(action))

    def test_a_reader_can_not_edit_findings_or_accept_risk(self):
        for action in (Action.FINDING_EDIT, Action.RISK_ACCEPT):
            with self.subTest(action=action):
                qualifying = self._roles_for(action)
                self.assertNotIn(Roles.Reader.value, qualifying)
                self.assertIn(Roles.Writer.value, qualifying)

    def test_operating_a_project_or_managing_an_org_needs_maintainer(self):
        for action in (
            Action.PROJECT_OPERATE,
            Action.PROJECT_CREATE,
            Action.ORG_MANAGE,
            Action.ORG_MANAGE_READ,
        ):
            with self.subTest(action=action):
                qualifying = self._roles_for(action)
                self.assertNotIn(Roles.Writer.value, qualifying)
                self.assertIn(Roles.Maintainer.value, qualifying)

    def test_granting_owner_is_owner_only(self):
        self.assertEqual(
            self._roles_for(Action.OWNER_GRANT),
            frozenset({Roles.Owner.value}),
        )

    def test_an_owner_satisfies_every_capability(self):
        for action in Action:
            with self.subTest(action=action):
                self.assertIn(Roles.Owner.value, self._roles_for(action))
