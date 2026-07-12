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

from aist.models import AISTProject, Organization
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

        # Product_Member WITHOUT org membership: must have NO access.
        self.pm_only = self._user("pm_only")
        Product_Member.objects.create(product=self.prod_a1, user=self.pm_only, role=self.role_owner)

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
