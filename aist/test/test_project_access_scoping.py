"""
Tests for the access model: ORG MEMBERSHIP IS REQUIRED, per-project grants NARROW.

- A member (Product_Type_Member) with no per-project grant sees all org projects.
- A member with per-project grants is narrowed to ONLY those projects.
- A Product_Member WITHOUT org membership grants NOTHING (no cross-boundary access).
- Grants never cross organizations.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from dojo.authorization.roles_permissions import Permissions, Roles
from dojo.models import (
    Engagement,
    Finding,
    Product,
    Product_Member,
    Product_Type,
    Product_Type_Member,
    Role,
    SLA_Configuration,
    Test,
    Test_Type,
)

from aist.models import AISTProject, Organization, OrgMemberAccessScope
from aist.queries import (
    get_authorized_aist_organizations,
    get_authorized_aist_products,
    get_authorized_aist_projects,
    get_authorized_findings,
    get_visible_aist_organizations,
)

User = get_user_model()


class ProjectAccessScopingTests(TestCase):
    def setUp(self):
        self.sla = SLA_Configuration.objects.create(name="SLA")
        self.role_reader, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        self.role_writer, _ = Role.objects.get_or_create(id=Roles.Writer, defaults={"name": "Writer"})
        self.role_owner, _ = Role.objects.get_or_create(id=Roles.Owner, defaults={"name": "Owner"})

        # Org A with two projects; Org B with one.
        self.pt_a = Product_Type.objects.create(name="Org A")
        self.org_a = Organization.objects.create(name="Org A", product_type=self.pt_a)
        self.prod_a1 = self._product("A1", self.pt_a)
        self.prod_a2 = self._product("A2", self.pt_a)
        self.proj_a1 = self._project(self.prod_a1)
        self.proj_a2 = self._project(self.prod_a2)

        self.pt_b = Product_Type.objects.create(name="Org B")
        self.org_b = Organization.objects.create(name="Org B", product_type=self.pt_b)
        self.prod_b1 = self._product("B1", self.pt_b)
        self.proj_b1 = self._project(self.prod_b1)

        # Full member of A (no per-project grant): sees all A projects.
        self.full_a = self._user("full_a")
        Product_Type_Member.objects.create(product_type=self.pt_a, user=self.full_a, role=self.role_reader)

        # Restricted member of A: org member (Reader) + a per-project grant on A1 only.
        self.restricted_a = self._user("restricted_a")
        Product_Type_Member.objects.create(product_type=self.pt_a, user=self.restricted_a, role=self.role_reader)
        Product_Member.objects.create(product=self.prod_a1, user=self.restricted_a, role=self.role_writer)
        OrgMemberAccessScope.objects.create(organization=self.org_a, user=self.restricted_a, restricted=True)

        # Product_Member WITHOUT org membership: must have NO access.
        self.pm_only = self._user("pm_only")
        Product_Member.objects.create(product=self.prod_a1, user=self.pm_only, role=self.role_owner)

        # Restricted member of A with ZERO project grants — the exact state a
        # full member ends up in after every project grant is revoked. Must
        # see nothing, not fall back to full org access.
        self.restricted_zero_a = self._user("restricted_zero_a")
        Product_Type_Member.objects.create(product_type=self.pt_a, user=self.restricted_zero_a, role=self.role_reader)
        OrgMemberAccessScope.objects.create(organization=self.org_a, user=self.restricted_zero_a, restricted=True)

        self.member_b = self._user("member_b")
        Product_Type_Member.objects.create(product_type=self.pt_b, user=self.member_b, role=self.role_reader)

        self.outsider = self._user("outsider")
        self.superuser = User.objects.create_superuser("root", "root@example.com", "pass")

    # ---- helpers -------------------------------------------------------

    def _product(self, name, prod_type):
        return Product.objects.create(
            name=name, description="d", prod_type=prod_type, sla_configuration_id=self.sla.id,
        )

    def _project(self, product):
        return AISTProject.objects.create(
            product=product, supported_languages=["python"], compilable=False, profile={},
        )

    def _user(self, username):
        return User.objects.create_user(username, f"{username}@example.com", "pass")

    def _finding(self, product, title):
        engagement = Engagement.objects.create(
            name=f"E-{title}", target_start=timezone.now(), target_end=timezone.now(), product=product,
        )
        test_type, _ = Test_Type.objects.get_or_create(name="Semgrep")
        test = Test.objects.create(
            engagement=engagement, target_start=timezone.now(), target_end=timezone.now(), test_type=test_type,
        )
        return Finding.objects.create(
            test=test, title=title, severity="High", date=timezone.now(), reporter=self.full_a,
        )

    def _product_ids(self, permission, user):
        return set(get_authorized_aist_products(permission, user=user).values_list("id", flat=True))

    # ---- membership is required ---------------------------------------

    def test_product_member_without_org_membership_has_no_access(self):
        # The core invariant: a per-project grant alone grants nothing.
        self.assertEqual(self._product_ids(Permissions.Product_View, self.pm_only), set())
        self.assertFalse(get_authorized_aist_projects(Permissions.Product_View, user=self.pm_only).exists())
        self.assertFalse(get_visible_aist_organizations(user=self.pm_only).exists())

    def test_outsider_sees_nothing(self):
        self.assertEqual(self._product_ids(Permissions.Product_View, self.outsider), set())

    # ---- narrowing -----------------------------------------------------

    def test_full_member_sees_all_org_products(self):
        ids = self._product_ids(Permissions.Product_View, self.full_a)
        self.assertEqual(ids, {self.prod_a1.id, self.prod_a2.id})

    def test_full_member_rogue_elevated_grant_is_ignored_not_honored(self):
        # A Product_Member row created by ANY path other than
        # OrganizationMembershipService._grant_project (e.g. vendor's own
        # product-member admin API, which doesn't enforce the "project role
        # can't exceed org role" cap) must never grant more than the org
        # role would. full_a's org role is Reader (rank 0); this rogue Owner
        # grant (rank 4) on prod_a2 must be treated as if it didn't exist —
        # prod_a2 still visible (Reader already grants View), but no Edit.
        Product_Member.objects.create(product=self.prod_a2, user=self.full_a, role=self.role_owner)
        self.assertEqual(
            self._product_ids(Permissions.Product_View, self.full_a), {self.prod_a1.id, self.prod_a2.id},
        )
        self.assertEqual(self._product_ids(Permissions.Finding_Edit, self.full_a), set())

    def test_full_member_conforming_downgrade_narrows_only_that_project(self):
        # The legitimate counterpart: a downgrade override (role rank BELOW
        # the org role) is honored for that one project, without affecting
        # the other, untouched project.
        owner_full = self._user("owner_full")
        Product_Type_Member.objects.create(product_type=self.pt_a, user=owner_full, role=self.role_owner)
        Product_Member.objects.create(product=self.prod_a2, user=owner_full, role=self.role_reader)
        self.assertEqual(
            self._product_ids(Permissions.Product_View, owner_full), {self.prod_a1.id, self.prod_a2.id},
        )
        # Edit still works on prod_a1 (plain org-role Owner), but not on the
        # downgraded prod_a2.
        self.assertEqual(self._product_ids(Permissions.Finding_Edit, owner_full), {self.prod_a1.id})

    def test_restricted_member_narrowed_to_granted_project(self):
        ids = self._product_ids(Permissions.Product_View, self.restricted_a)
        self.assertEqual(ids, {self.prod_a1.id})  # NOT prod_a2, despite org membership

    def test_restricted_member_cross_org_isolation(self):
        ids = self._product_ids(Permissions.Product_View, self.restricted_a)
        self.assertNotIn(self.prod_b1.id, ids)

    def test_read_vs_write_scoping_for_restricted_grant(self):
        # Restricted grant is Writer → qualifies for Finding_Edit on A1 only.
        edit_ids = self._product_ids(Permissions.Finding_Edit, self.restricted_a)
        self.assertEqual(edit_ids, {self.prod_a1.id})

    def test_restricted_reader_grant_has_no_write(self):
        reader_restricted = self._user("reader_restricted")
        Product_Type_Member.objects.create(product_type=self.pt_a, user=reader_restricted, role=self.role_reader)
        Product_Member.objects.create(product=self.prod_a1, user=reader_restricted, role=self.role_reader)
        OrgMemberAccessScope.objects.create(organization=self.org_a, user=reader_restricted, restricted=True)
        self.assertEqual(self._product_ids(Permissions.Product_View, reader_restricted), {self.prod_a1.id})
        self.assertEqual(self._product_ids(Permissions.Finding_Edit, reader_restricted), set())

    def test_superuser_sees_all_products(self):
        ids = self._product_ids(Permissions.Product_View, self.superuser)
        self.assertEqual(ids, {self.prod_a1.id, self.prod_a2.id, self.prod_b1.id})

    # ---- chained getters ----------------------------------------------

    def test_projects_getter_matches_products(self):
        projects = get_authorized_aist_projects(Permissions.Product_View, user=self.restricted_a)
        self.assertEqual(set(projects.values_list("id", flat=True)), {self.proj_a1.id})

    def test_findings_getter_scoped_to_narrowed_access(self):
        f_a1 = self._finding(self.prod_a1, "in-scope")
        f_a2 = self._finding(self.prod_a2, "out-of-scope-same-org")
        f_b1 = self._finding(self.prod_b1, "other-org")
        finding_ids = set(
            get_authorized_findings(Permissions.Finding_View, user=self.restricted_a).values_list("id", flat=True),
        )
        self.assertEqual(finding_ids, {f_a1.id})
        self.assertNotIn(f_a2.id, finding_ids)
        self.assertNotIn(f_b1.id, finding_ids)

    def test_findings_getter_empty_for_member_restricted_to_no_projects(self):
        # get_authorized_findings delegates to get_authorized_aist_products, so
        # this should pass "for free" once that function is fixed — which is
        # exactly why it matters: it proves the fix is a genuine queryset-level
        # fix, not something that only happens to work for the Members list
        # API, and that findings (the actual sensitive data) don't leak through
        # a separate path.
        self._finding(self.prod_a1, "should-not-be-visible")
        self._finding(self.prod_a2, "should-not-be-visible-either")
        finding_ids = set(
            get_authorized_findings(Permissions.Finding_View, user=self.restricted_zero_a).values_list("id", flat=True),
        )
        self.assertEqual(finding_ids, set())
        self.assertEqual(self._product_ids(Permissions.Product_View, self.restricted_zero_a), set())

    # ---- organization visibility vs management ------------------------

    def test_visible_orgs_for_full_and_restricted_members(self):
        self.assertEqual(
            set(get_visible_aist_organizations(user=self.full_a).values_list("id", flat=True)), {self.org_a.id},
        )
        self.assertEqual(
            set(get_visible_aist_organizations(user=self.restricted_a).values_list("id", flat=True)), {self.org_a.id},
        )

    def test_management_getter_requires_manager_role(self):
        # A Reader (full member) is not a manager.
        self.assertFalse(
            get_authorized_aist_organizations(Permissions.Product_Type_Manage_Members, user=self.full_a).exists(),
        )


class MultiOrgMembershipTests(ProjectAccessScopingTests):

    """
    Every fixture in ProjectAccessScopingTests belongs to at most ONE org —
    that leaves the genuinely multi-org case (one real user, membership rows
    in 2+ organizations at once, possibly with different roles/narrowing in
    each) completely unexercised. These tests build that case explicitly and
    check every getter still scopes correctly per-org for that single user,
    instead of only ever comparing across DIFFERENT single-org users.

    Inherits setUp from ProjectAccessScopingTests to reuse its orgs/products.
    """

    def setUp(self):
        super().setUp()
        # One real person, full member of org_a (Reader) AND org_b (Reader).
        self.multi_full = self._user("multi_full")
        Product_Type_Member.objects.create(product_type=self.pt_a, user=self.multi_full, role=self.role_reader)
        Product_Type_Member.objects.create(product_type=self.pt_b, user=self.multi_full, role=self.role_reader)

        # One person, restricted (A1 only) in org_a, but a FULL member of org_b.
        # Narrowing in one org must not bleed into (or be relaxed by) the other.
        self.multi_mixed = self._user("multi_mixed")
        Product_Type_Member.objects.create(product_type=self.pt_a, user=self.multi_mixed, role=self.role_reader)
        Product_Member.objects.create(product=self.prod_a1, user=self.multi_mixed, role=self.role_writer)
        OrgMemberAccessScope.objects.create(organization=self.org_a, user=self.multi_mixed, restricted=True)
        Product_Type_Member.objects.create(product_type=self.pt_b, user=self.multi_mixed, role=self.role_reader)

        # One person, Owner in org_a but only Reader in org_b — management
        # permission must be evaluated per-org, not as a single blanket grant.
        self.multi_owner_a_reader_b = self._user("multi_owner_a_reader_b")
        Product_Type_Member.objects.create(
            product_type=self.pt_a, user=self.multi_owner_a_reader_b, role=self.role_owner,
        )
        Product_Type_Member.objects.create(
            product_type=self.pt_b, user=self.multi_owner_a_reader_b, role=self.role_reader,
        )

    def test_full_member_of_two_orgs_sees_products_from_both(self):
        ids = self._product_ids(Permissions.Product_View, self.multi_full)
        self.assertEqual(ids, {self.prod_a1.id, self.prod_a2.id, self.prod_b1.id})

    def test_visible_orgs_for_member_of_two_orgs_returns_both(self):
        org_ids = set(get_visible_aist_organizations(user=self.multi_full).values_list("id", flat=True))
        self.assertEqual(org_ids, {self.org_a.id, self.org_b.id})

    def test_narrowing_in_one_org_does_not_bleed_into_full_membership_in_another(self):
        # Restricted to A1 in org_a, but a FULL member of org_b -> must see
        # A1 (grant) + all of org_b (B1), and must NOT see A2 (org_a's other
        # project, correctly excluded despite org_b granting full access).
        ids = self._product_ids(Permissions.Product_View, self.multi_mixed)
        self.assertEqual(ids, {self.prod_a1.id, self.prod_b1.id})
        self.assertNotIn(self.prod_a2.id, ids)

    def test_narrowing_in_one_org_does_not_gain_write_from_full_membership_in_another(self):
        # Full Reader membership in org_b must not grant Finding_Edit on org_b's
        # product just because the same user has a Writer grant elsewhere (A1).
        edit_ids = self._product_ids(Permissions.Finding_Edit, self.multi_mixed)
        self.assertEqual(edit_ids, {self.prod_a1.id})
        self.assertNotIn(self.prod_b1.id, edit_ids)

    def test_findings_for_member_of_two_orgs_combine_both_without_bleed(self):
        f_a1 = self._finding(self.prod_a1, "org-a-in-scope")
        f_a2 = self._finding(self.prod_a2, "org-a-out-of-scope")
        f_b1 = self._finding(self.prod_b1, "org-b-in-scope")
        finding_ids = set(
            get_authorized_findings(Permissions.Finding_View, user=self.multi_mixed).values_list("id", flat=True),
        )
        self.assertEqual(finding_ids, {f_a1.id, f_b1.id})
        self.assertNotIn(f_a2.id, finding_ids)

    def test_management_permission_evaluated_independently_per_org(self):
        # Owner in org_a, plain Reader in org_b: must be able to manage org_a
        # only — the Owner role in one org must not implicitly grant
        # management rights in another org the same person happens to belong to.
        manageable = set(
            get_authorized_aist_organizations(
                Permissions.Product_Type_Manage_Members, user=self.multi_owner_a_reader_b,
            ).values_list("id", flat=True),
        )
        self.assertEqual(manageable, {self.org_a.id})
        self.assertNotIn(self.org_b.id, manageable)
