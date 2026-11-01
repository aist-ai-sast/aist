"""
DefectDojo's own finding authorization answers with AIST's tenancy.

Vendor code that re-authorizes a queryset resolves it through
``dojo.authorization.query_filters``. Its default reads ``Product.authorized_users``,
which AIST never writes, so an unregistered filter denies every non-superuser.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from dojo.authorization.query_filters import get_auth_filter
from dojo.authorization.roles_permissions import Roles
from dojo.finding.queries import get_authorized_findings, get_authorized_findings_for_queryset
from dojo.models import (
    Engagement,
    Finding,
    Product,
    Product_Type,
    Product_Type_Member,
    Role,
    SLA_Configuration,
    Test,
    Test_Type,
)

from aist.models import Organization

User = get_user_model()


class VendorFindingFilterTests(TestCase):
    def setUp(self):
        self.sla = SLA_Configuration.objects.create(name="SLA")
        self.roles = {
            role: Role.objects.get_or_create(id=role, defaults={"name": role.name})[0]
            for role in (Roles.Reader, Roles.Writer, Roles.Owner)
        }
        self.mine, self.my_finding = self._tenant("Mine")
        self.theirs, self.their_finding = self._tenant("Theirs")

    def _tenant(self, label):
        product_type = Product_Type.objects.create(name=label)
        Organization.objects.create(name=label, product_type=product_type)
        product = Product.objects.create(
            name=label, description="d", prod_type=product_type,
            sla_configuration_id=self.sla.id,
        )
        engagement = Engagement.objects.create(
            name=f"eng-{label}", target_start=timezone.now(), target_end=timezone.now(),
            product=product,
        )
        test_type, _ = Test_Type.objects.get_or_create(name="Semgrep JSON Report")
        test = Test.objects.create(
            engagement=engagement, test_type=test_type,
            target_start=timezone.now(), target_end=timezone.now(),
        )
        reporter = User.objects.create_user(f"rep-{label}", f"rep-{label}@x.test", "pass")
        finding = Finding.objects.create(
            test=test, title=f"f-{label}", severity="High", description="d",
            numerical_severity="S1", date=timezone.now(), reporter=reporter,
        )
        return product_type, finding

    def _member(self, username, product_type, role):
        user = User.objects.create_user(username, f"{username}@x.test", "pass")
        Product_Type_Member.objects.create(
            product_type=product_type, user=user, role=self.roles[role],
        )
        return user

    def test_the_vendor_resolves_findings_through_our_filter(self):
        impl = get_auth_filter("finding.get_authorized_findings")
        self.assertEqual(impl.__module__, "aist.authz.vendor_filters")

    def test_a_reader_may_view_but_not_edit(self):
        """The ladder governs the vendor's own query, not just AIST's endpoints."""
        reader = self._member("vf_reader", self.mine, Roles.Reader)
        self.assertIn(self.my_finding, get_authorized_findings("view", user=reader))
        self.assertNotIn(self.my_finding, get_authorized_findings("edit", user=reader))

    def test_a_writer_may_edit(self):
        writer = self._member("vf_writer", self.mine, Roles.Writer)
        self.assertIn(self.my_finding, get_authorized_findings("edit", user=writer))

    def test_no_role_reaches_another_organization(self):
        owner = self._member("vf_owner", self.mine, Roles.Owner)
        for action in ("view", "edit"):
            with self.subTest(action=action):
                reachable = get_authorized_findings(action, user=owner)
                self.assertNotIn(self.their_finding, reachable)

    def test_a_non_member_reaches_nothing(self):
        outsider = User.objects.create_user("vf_outsider", "vf_out@x.test", "pass")
        self.assertFalse(get_authorized_findings("view", user=outsider).exists())

    def test_a_superuser_can_still_bulk_delete(self):
        """``_bulk_delete_findings`` asks with "delete"; an unmapped intent deletes nothing."""
        superuser = User.objects.create_superuser("vf_super", "vf_super@x.test", "pass")
        reachable = get_authorized_findings_for_queryset("delete", Finding.objects.all(), user=superuser)
        self.assertIn(self.my_finding, reachable)
        self.assertIn(self.their_finding, reachable)

    def test_a_writer_may_delete_in_their_own_organization_only(self):
        writer = self._member("vf_deleter", self.mine, Roles.Writer)
        reachable = get_authorized_findings_for_queryset("delete", Finding.objects.all(), user=writer)
        self.assertIn(self.my_finding, reachable)
        self.assertNotIn(self.their_finding, reachable)

    def test_an_intent_with_no_role_equivalent_grants_nothing(self):
        """``staff_only`` has no place on the AIST ladder, so it must not fall through."""
        owner = self._member("vf_staffcheck", self.mine, Roles.Owner)
        self.assertFalse(get_authorized_findings("staff_only", user=owner).exists())
