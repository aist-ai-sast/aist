"""
Every role against every capability, end to end through the real scoped querysets.

Real memberships are driven through ``aist.queries`` in three shapes: a direct
organization membership, a group-granted one, and a restricted member's per-project
grant. ``EXPECTED`` below is the published capability table in executable form.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from dojo.authorization.roles_permissions import Roles
from dojo.models import (
    Dojo_Group,
    Dojo_Group_Member,
    Product,
    Product_Member,
    Product_Type,
    Product_Type_Group,
    Product_Type_Member,
    Role,
    SLA_Configuration,
)

from aist.authz.policy import ACTION_PERMISSIONS, Action
from aist.models import AISTProject, Organization, OrgMemberAccessScope
from aist.queries import get_authorized_aist_organizations, get_authorized_aist_products

User = get_user_model()

ALL_ROLES = (Roles.Reader, Roles.API_Importer, Roles.Writer, Roles.Maintainer, Roles.Owner)
READER_UP = frozenset(ALL_ROLES)
WRITER_UP = frozenset({Roles.Writer, Roles.Maintainer, Roles.Owner})
MAINTAINER_UP = frozenset({Roles.Maintainer, Roles.Owner})
OWNER_ONLY = frozenset({Roles.Owner})

# The published capability table. One row = one documented guarantee.
EXPECTED: dict[Action, frozenset] = {
    Action.PRODUCT_READ: READER_UP,
    Action.FINDING_READ: READER_UP,
    Action.TEST_READ: READER_UP,
    Action.ENGAGEMENT_READ: READER_UP,
    Action.ORG_READ: READER_UP,
    Action.FINDING_EDIT: WRITER_UP,
    Action.RISK_ACCEPT: WRITER_UP,
    Action.ORG_MANAGE_READ: MAINTAINER_UP,
    Action.PROJECT_OPERATE: MAINTAINER_UP,
    Action.PROJECT_CREATE: MAINTAINER_UP,
    Action.ORG_MANAGE: MAINTAINER_UP,
    Action.OWNER_GRANT: OWNER_ONLY,
}


class RoleCapabilityMatrixBase(TestCase):
    def setUp(self):
        self.sla = SLA_Configuration.objects.create(name="SLA")
        self.roles = {
            role: Role.objects.get_or_create(id=role, defaults={"name": role.name})[0]
            for role in ALL_ROLES
        }
        self.product_type = Product_Type.objects.create(name="Org")
        self.organization = Organization.objects.create(name="Org", product_type=self.product_type)
        self.product = Product.objects.create(
            name="P", description="d", prod_type=self.product_type,
            sla_configuration_id=self.sla.id,
        )
        AISTProject.objects.create(
            product=self.product, supported_languages=["python"], compilable=False, profile={},
        )

    def _user(self, name):
        return User.objects.create_user(name, f"{name}@example.com", "pass")

    def _reaches_product(self, action: Action, user) -> bool:
        permission = ACTION_PERMISSIONS[action]
        return get_authorized_aist_products(permission, user=user).filter(pk=self.product.pk).exists()

    def _reaches_organization(self, action: Action, user) -> bool:
        permission = ACTION_PERMISSIONS[action]
        return get_authorized_aist_organizations(permission, user=user).filter(
            pk=self.organization.pk,
        ).exists()

    def _assert_matrix(self, users_by_role: dict):
        """Both scoped getters must agree with the published table, for every pair."""
        for action, allowed in EXPECTED.items():
            for role in ALL_ROLES:
                expected = role in allowed
                with self.subTest(action=action.value, role=role.name, scope="product"):
                    self.assertEqual(self._reaches_product(action, users_by_role[role]), expected)
                with self.subTest(action=action.value, role=role.name, scope="organization"):
                    self.assertEqual(
                        self._reaches_organization(action, users_by_role[role]), expected,
                    )


class DirectOrganizationMembershipMatrixTests(RoleCapabilityMatrixBase):

    """A plain ``Product_Type_Member`` at each role."""

    def setUp(self):
        super().setUp()
        self.users = {}
        for role in ALL_ROLES:
            user = self._user(f"direct_{role.name.lower()}")
            Product_Type_Member.objects.create(
                product_type=self.product_type, user=user, role=self.roles[role],
            )
            self.users[role] = user

    def test_every_role_reaches_exactly_its_documented_capabilities(self):
        self._assert_matrix(self.users)

    def test_an_api_importer_reads_but_can_not_mutate_findings(self):
        """Pinned deliberately: API_Importer outranks Reader but is below Writer."""
        user = self.users[Roles.API_Importer]
        self.assertTrue(self._reaches_product(Action.FINDING_READ, user))
        self.assertFalse(self._reaches_product(Action.FINDING_EDIT, user))

    def test_a_non_member_reaches_nothing_at_any_capability(self):
        outsider = self._user("outsider")
        for action in EXPECTED:
            with self.subTest(action=action.value):
                self.assertFalse(self._reaches_product(action, outsider))
                self.assertFalse(self._reaches_organization(action, outsider))

    def test_a_superuser_reaches_everything(self):
        root = User.objects.create_superuser("root", "root@example.com", "pass")
        for action in EXPECTED:
            with self.subTest(action=action.value):
                self.assertTrue(self._reaches_product(action, root))
                self.assertTrue(self._reaches_organization(action, root))


class GroupGrantedMembershipMatrixTests(RoleCapabilityMatrixBase):

    """Membership through ``Product_Type_Group`` — a separate role-filtered branch."""

    def setUp(self):
        super().setUp()
        self.users = {}
        for role in ALL_ROLES:
            user = self._user(f"group_{role.name.lower()}")
            group = Dojo_Group.objects.create(name=f"grp_{role.name.lower()}")
            Dojo_Group_Member.objects.create(group=group, user=user, role=self.roles[Roles.Reader])
            Product_Type_Group.objects.create(
                product_type=self.product_type, group=group, role=self.roles[role],
            )
            self.users[role] = user

    def test_a_group_grant_confers_exactly_the_same_capabilities_as_a_direct_one(self):
        self._assert_matrix(self.users)


class RestrictedMemberProjectGrantMatrixTests(RoleCapabilityMatrixBase):

    """A restricted member reaches a project only through its explicit grant's role."""

    def setUp(self):
        super().setUp()
        self.users = {}
        for role in ALL_ROLES:
            user = self._user(f"restricted_{role.name.lower()}")
            # Baseline org membership is Owner so the cap never masks the grant's role:
            # what is under test is the per-project grant, not the organization role.
            Product_Type_Member.objects.create(
                product_type=self.product_type, user=user, role=self.roles[Roles.Owner],
            )
            OrgMemberAccessScope.objects.create(
                organization=self.organization, user=user, restricted=True,
            )
            Product_Member.objects.create(
                product=self.product, user=user, role=self.roles[role],
            )
            self.users[role] = user

    def test_the_grants_role_decides_what_the_restricted_member_can_do(self):
        for action, allowed in EXPECTED.items():
            for role in ALL_ROLES:
                with self.subTest(action=action.value, grant_role=role.name):
                    self.assertEqual(
                        self._reaches_product(action, self.users[role]),
                        role in allowed,
                    )

    def test_a_restricted_member_without_any_grant_reaches_nothing(self):
        user = self._user("restricted_nograntee")
        Product_Type_Member.objects.create(
            product_type=self.product_type, user=user, role=self.roles[Roles.Owner],
        )
        OrgMemberAccessScope.objects.create(
            organization=self.organization, user=user, restricted=True,
        )
        for action in EXPECTED:
            with self.subTest(action=action.value):
                self.assertFalse(self._reaches_product(action, user))


class MultiTenancyMatrixTests(RoleCapabilityMatrixBase):

    """Tenancy rests on ``Organization.product_type``; no role may cross that line."""

    def setUp(self):
        super().setUp()
        self.other_product_type = Product_Type.objects.create(name="Other Org")
        self.other_organization = Organization.objects.create(
            name="Other Org", product_type=self.other_product_type,
        )
        self.other_product = Product.objects.create(
            name="Other P", description="d", prod_type=self.other_product_type,
            sla_configuration_id=self.sla.id,
        )
        AISTProject.objects.create(
            product=self.other_product, supported_languages=["python"],
            compilable=False, profile={},
        )
        self.users = {}
        for role in ALL_ROLES:
            user = self._user(f"tenant_{role.name.lower()}")
            Product_Type_Member.objects.create(
                product_type=self.product_type, user=user, role=self.roles[role],
            )
            self.users[role] = user

    def _reaches_other_product(self, action: Action, user) -> bool:
        permission = ACTION_PERMISSIONS[action]
        return get_authorized_aist_products(permission, user=user).filter(
            pk=self.other_product.pk,
        ).exists()

    def _reaches_other_organization(self, action: Action, user) -> bool:
        permission = ACTION_PERMISSIONS[action]
        return get_authorized_aist_organizations(permission, user=user).filter(
            pk=self.other_organization.pk,
        ).exists()

    def test_no_role_reaches_another_organization_at_any_capability(self):
        """Even an Owner is an Owner of one organization only."""
        for action in EXPECTED:
            for role in ALL_ROLES:
                with self.subTest(action=action.value, role=role.name):
                    self.assertFalse(self._reaches_other_product(action, self.users[role]))
                    self.assertFalse(self._reaches_other_organization(action, self.users[role]))

    def test_an_owner_of_both_organizations_is_scoped_to_each_separately(self):
        """Two memberships must add up, not merge into one borderless scope."""
        user = self._user("owner_of_both")
        Product_Type_Member.objects.create(
            product_type=self.product_type, user=user, role=self.roles[Roles.Owner],
        )
        Product_Type_Member.objects.create(
            product_type=self.other_product_type, user=user, role=self.roles[Roles.Reader],
        )
        # Owner in the first organization, Reader in the second: the capability a
        # product is reachable at follows the organization it belongs to.
        self.assertTrue(self._reaches_product(Action.PROJECT_OPERATE, user))
        self.assertFalse(self._reaches_other_product(Action.PROJECT_OPERATE, user))
        self.assertTrue(self._reaches_product(Action.PRODUCT_READ, user))
        self.assertTrue(self._reaches_other_product(Action.PRODUCT_READ, user))

    def test_a_product_type_that_is_not_an_aist_organization_is_not_an_organization(self):
        """Tenancy is the ``Organization`` row, not bare Product_Type membership."""
        unbound = Product_Type.objects.create(name="Vendor-only type")
        user = self._user("vendor_only_member")
        Product_Type_Member.objects.create(
            product_type=unbound, user=user, role=self.roles[Roles.Owner],
        )
        for action in EXPECTED:
            with self.subTest(action=action.value):
                self.assertEqual(
                    get_authorized_aist_organizations(
                        ACTION_PERMISSIONS[action], user=user,
                    ).count(),
                    0,
                )

    def test_an_organization_can_not_share_a_product_type_with_another(self):
        """The OneToOne is what makes 'one tenant' well defined."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            Organization.objects.create(name="Impostor", product_type=self.product_type)
