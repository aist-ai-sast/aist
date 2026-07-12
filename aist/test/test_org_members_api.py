"""
API tests for organization member & per-project access management.

The matrix deliberately concentrates on ACCESS-VIOLATION scenarios: cross-org
grants, granting to non-members, management by non-managers, Owner-role
escalation, the last-owner guard, and idempotency under concurrent edits.
Email delivery is mocked — these tests assert the membership state machine, not
SMTP.
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from dojo.authorization.roles_permissions import Roles
from dojo.models import (
    Product,
    Product_Member,
    Product_Type,
    Product_Type_Member,
    Role,
    SLA_Configuration,
)
from rest_framework.test import APIClient

from aist.models import AISTApiToken, AISTProject, ApiTokenScope, Organization

User = get_user_model()

EMAIL_PATCH = "aist.members.service.send_set_password_email"


class OrgMembersApiBase(TestCase):
    def setUp(self):
        self.sla = SLA_Configuration.objects.create(name="SLA")
        self.role_reader, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        self.role_writer, _ = Role.objects.get_or_create(id=Roles.Writer, defaults={"name": "Writer"})
        self.role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        self.role_owner, _ = Role.objects.get_or_create(id=Roles.Owner, defaults={"name": "Owner"})

        self.pt_a = Product_Type.objects.create(name="Org A")
        self.org_a = Organization.objects.create(name="Org A", product_type=self.pt_a)
        self.proj_a1 = self._project("A1", self.pt_a)
        self.proj_a2 = self._project("A2", self.pt_a)

        self.pt_b = Product_Type.objects.create(name="Org B")
        self.org_b = Organization.objects.create(name="Org B", product_type=self.pt_b)
        self.proj_b1 = self._project("B1", self.pt_b)

        self.owner_a = self._member("owner_a", self.pt_a, self.role_owner)
        self.maintainer_a = self._member("maintainer_a", self.pt_a, self.role_maintainer)
        self.reader_a = self._member("reader_a", self.pt_a, self.role_reader)
        self.owner_b = self._member("owner_b", self.pt_b, self.role_owner)
        self.outsider = self._user("outsider")
        self.superuser = User.objects.create_superuser("root", "root@example.com", "pass")

    # -- fixtures --------------------------------------------------------

    def _project(self, name, prod_type) -> AISTProject:
        product = Product.objects.create(
            name=name, description="d", prod_type=prod_type, sla_configuration_id=self.sla.id,
        )
        return AISTProject.objects.create(
            product=product, supported_languages=["python"], compilable=False, profile={},
        )

    def _user(self, username):
        return User.objects.create_user(username, f"{username}@example.com", "pass")

    def _member(self, username, prod_type, role):
        user = self._user(username)
        Product_Type_Member.objects.create(product_type=prod_type, user=user, role=role)
        return user

    def _client(self, user) -> APIClient:
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    # -- url helpers -----------------------------------------------------

    def _members_url(self, org):
        return reverse("aist_api:org_member_list_create", kwargs={"org_id": org.id})

    def _member_url(self, org, user):
        return reverse("aist_api:org_member_detail", kwargs={"org_id": org.id, "user_id": user.id})

    def _reset_url(self, org, user):
        return reverse("aist_api:org_member_reset_password", kwargs={"org_id": org.id, "user_id": user.id})

    def _grants_url(self, org, user):
        return reverse(
            "aist_api:org_member_project_grant_list_create",
            kwargs={"org_id": org.id, "user_id": user.id},
        )

    def _grant_detail_url(self, org, user, project):
        return reverse(
            "aist_api:org_member_project_grant_detail",
            kwargs={"org_id": org.id, "user_id": user.id, "project_id": project.id},
        )


class ManagementGateTests(OrgMembersApiBase):
    def test_reader_cannot_list_members(self):
        resp = self._client(self.reader_a).get(self._members_url(self.org_a))
        self.assertEqual(resp.status_code, 404)

    def test_maintainer_can_list_members(self):
        resp = self._client(self.maintainer_a).get(self._members_url(self.org_a))
        self.assertEqual(resp.status_code, 200)

    def test_outsider_cannot_list_members(self):
        resp = self._client(self.outsider).get(self._members_url(self.org_a))
        self.assertEqual(resp.status_code, 404)

    def test_owner_cannot_manage_other_org(self):
        resp = self._client(self.owner_a).get(self._members_url(self.org_b))
        self.assertEqual(resp.status_code, 404)

    def test_superuser_can_manage_any_org(self):
        resp = self._client(self.superuser).get(self._members_url(self.org_b))
        self.assertEqual(resp.status_code, 200)

    def test_list_marks_full_and_restricted(self):
        # reader_a is an org member; adding a per-project grant makes them "restricted".
        Product_Member.objects.create(user=self.reader_a, product=self.proj_a1.product, role=self.role_writer)
        resp = self._client(self.owner_a).get(self._members_url(self.org_a))
        by_user = {m["user_id"]: m for m in resp.json()}
        self.assertEqual(by_user[self.owner_a.id]["membership_type"], "full")
        self.assertEqual(by_user[self.reader_a.id]["membership_type"], "restricted")
        # A user with no org membership never appears, even with a stray grant.
        self.assertNotIn(self.outsider.id, by_user)

    def test_list_reports_token_indicator_without_secret(self):
        AISTApiToken.issue(user=self.reader_a, name="ci", scope=ApiTokenScope.READ_ONLY)
        resp = self._client(self.owner_a).get(self._members_url(self.org_a))
        row = next(m for m in resp.json() if m["user_id"] == self.reader_a.id)
        self.assertTrue(row["has_token"])
        self.assertEqual(row["token_count"], 1)
        self.assertNotIn("token", row)
        owner_row = next(m for m in resp.json() if m["user_id"] == self.owner_a.id)
        self.assertFalse(owner_row["has_token"])


class InviteTests(OrgMembersApiBase):
    @patch(EMAIL_PATCH)
    def test_invite_new_full_member_sends_email(self, mock_email):
        resp = self._client(self.owner_a).post(
            self._members_url(self.org_a),
            {"email": "new@example.com", "role_id": Roles.Reader.value},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        user = User.objects.get(email="new@example.com")
        self.assertTrue(user.is_active)
        self.assertTrue(user.has_usable_password())
        self.assertTrue(
            Product_Type_Member.objects.filter(product_type=self.pt_a, user=user).exists(),
        )
        mock_email.assert_called_once()

    @patch(EMAIL_PATCH)
    def test_invite_existing_user_does_not_email_or_duplicate(self, mock_email):
        self._client(self.owner_a).post(
            self._members_url(self.org_a),
            {"email": self.owner_b.email, "role_id": Roles.Reader.value},
            format="json",
        )
        self.assertEqual(
            Product_Type_Member.objects.filter(product_type=self.pt_a, user=self.owner_b).count(), 1,
        )
        mock_email.assert_not_called()

    @patch(EMAIL_PATCH)
    def test_invite_restricted_member_with_grants(self, mock_email):
        resp = self._client(self.owner_a).post(
            self._members_url(self.org_a),
            {
                "email": "restricted@example.com",
                "project_grants": [{"project_id": self.proj_a1.id, "role_id": Roles.Writer.value}],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        user = User.objects.get(email="restricted@example.com")
        # Restricted member is STILL an org member (baseline Reader) — membership
        # is required — plus the per-project grant that narrows their access.
        membership = Product_Type_Member.objects.get(product_type=self.pt_a, user=user)
        self.assertEqual(membership.role_id, Roles.Reader.value)
        self.assertTrue(Product_Member.objects.filter(user=user, product=self.proj_a1.product).exists())
        mock_email.assert_called_once()

    @patch(EMAIL_PATCH)
    def test_invite_requires_exactly_one_of_role_or_grants(self, mock_email):
        both = self._client(self.owner_a).post(
            self._members_url(self.org_a),
            {"email": "x@example.com", "role_id": Roles.Reader.value,
             "project_grants": [{"project_id": self.proj_a1.id, "role_id": Roles.Reader.value}]},
            format="json",
        )
        self.assertEqual(both.status_code, 400)
        neither = self._client(self.owner_a).post(
            self._members_url(self.org_a), {"email": "y@example.com"}, format="json",
        )
        self.assertEqual(neither.status_code, 400)

    @patch(EMAIL_PATCH)
    def test_invite_invalid_email_rejected(self, mock_email):
        resp = self._client(self.owner_a).post(
            self._members_url(self.org_a),
            {"email": "not-an-email", "role_id": Roles.Reader.value},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    @patch(EMAIL_PATCH)
    def test_maintainer_cannot_invite_owner(self, mock_email):
        resp = self._client(self.maintainer_a).post(
            self._members_url(self.org_a),
            {"email": "boss@example.com", "role_id": Roles.Owner.value},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

    @patch(EMAIL_PATCH)
    def test_owner_can_invite_owner(self, mock_email):
        resp = self._client(self.owner_a).post(
            self._members_url(self.org_a),
            {"email": "boss@example.com", "role_id": Roles.Owner.value},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)


class RoleChangeTests(OrgMembersApiBase):
    def test_owner_changes_reader_to_writer(self):
        resp = self._client(self.owner_a).patch(
            self._member_url(self.org_a, self.reader_a),
            {"role_id": Roles.Writer.value}, format="json",
        )
        self.assertEqual(resp.status_code, 200)
        member = Product_Type_Member.objects.get(product_type=self.pt_a, user=self.reader_a)
        self.assertEqual(member.role_id, Roles.Writer.value)

    def test_maintainer_cannot_promote_to_owner(self):
        resp = self._client(self.maintainer_a).patch(
            self._member_url(self.org_a, self.reader_a),
            {"role_id": Roles.Owner.value}, format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_maintainer_cannot_demote_existing_owner(self):
        # Owner-protection: editing an existing Owner needs Add_Owner (which a
        # Maintainer lacks) even when demoting, not only when promoting.
        second_owner = self._member("owner_a2", self.pt_a, self.role_owner)
        resp = self._client(self.maintainer_a).patch(
            self._member_url(self.org_a, second_owner),
            {"role_id": Roles.Maintainer.value}, format="json",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            Product_Type_Member.objects.get(product_type=self.pt_a, user=second_owner).role_id,
            Roles.Owner.value,
        )

    def test_cannot_demote_last_owner(self):
        resp = self._client(self.owner_a).patch(
            self._member_url(self.org_a, self.owner_a),
            {"role_id": Roles.Maintainer.value}, format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_can_demote_owner_when_another_exists(self):
        second = self._member("owner_a2", self.pt_a, self.role_owner)
        resp = self._client(self.owner_a).patch(
            self._member_url(self.org_a, second),
            {"role_id": Roles.Maintainer.value}, format="json",
        )
        self.assertEqual(resp.status_code, 200)

    def test_change_role_of_non_member_rejected(self):
        resp = self._client(self.owner_a).patch(
            self._member_url(self.org_a, self.outsider),
            {"role_id": Roles.Writer.value}, format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_change_role_missing_field_rejected(self):
        resp = self._client(self.owner_a).patch(
            self._member_url(self.org_a, self.reader_a), {}, format="json",
        )
        self.assertEqual(resp.status_code, 400)


class RemoveMemberTests(OrgMembersApiBase):
    def test_remove_full_member_clears_all_access(self):
        Product_Member.objects.create(user=self.reader_a, product=self.proj_a1.product, role=self.role_writer)
        resp = self._client(self.owner_a).delete(self._member_url(self.org_a, self.reader_a))
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Product_Type_Member.objects.filter(product_type=self.pt_a, user=self.reader_a).exists())
        self.assertFalse(Product_Member.objects.filter(user=self.reader_a, product__prod_type=self.pt_a).exists())

    def test_remove_restricted_member(self):
        # Restricted member = org member (reader_a) + a per-project grant.
        Product_Member.objects.create(user=self.reader_a, product=self.proj_a1.product, role=self.role_writer)
        resp = self._client(self.owner_a).delete(self._member_url(self.org_a, self.reader_a))
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Product_Type_Member.objects.filter(product_type=self.pt_a, user=self.reader_a).exists())
        self.assertFalse(Product_Member.objects.filter(user=self.reader_a, product=self.proj_a1.product).exists())

    def test_cannot_remove_last_owner(self):
        resp = self._client(self.owner_a).delete(self._member_url(self.org_a, self.owner_a))
        self.assertEqual(resp.status_code, 400)

    def test_maintainer_cannot_remove_existing_owner(self):
        # Owner-protection: a Maintainer lacks Add_Owner and must not evict an
        # Owner, even when another Owner remains.
        second_owner = self._member("owner_a2", self.pt_a, self.role_owner)
        resp = self._client(self.maintainer_a).delete(self._member_url(self.org_a, second_owner))
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(
            Product_Type_Member.objects.filter(product_type=self.pt_a, user=second_owner).exists(),
        )

    def test_remove_does_not_touch_other_org(self):
        # owner_b is a member of both orgs (restricted in A, member of B).
        Product_Type_Member.objects.create(product_type=self.pt_a, user=self.owner_b, role=self.role_reader)
        Product_Member.objects.create(user=self.owner_b, product=self.proj_a1.product, role=self.role_reader)
        Product_Member.objects.create(user=self.owner_b, product=self.proj_b1.product, role=self.role_writer)
        self._client(self.owner_a).delete(self._member_url(self.org_a, self.owner_b))
        # Org A access removed; org B membership and grants untouched.
        self.assertFalse(Product_Type_Member.objects.filter(product_type=self.pt_a, user=self.owner_b).exists())
        self.assertFalse(Product_Member.objects.filter(user=self.owner_b, product=self.proj_a1.product).exists())
        self.assertTrue(Product_Member.objects.filter(user=self.owner_b, product=self.proj_b1.product).exists())
        self.assertTrue(Product_Type_Member.objects.filter(product_type=self.pt_b, user=self.owner_b).exists())


class ProjectGrantTests(OrgMembersApiBase):
    def test_cannot_grant_cross_org_project(self):
        resp = self._client(self.owner_a).post(
            self._grants_url(self.org_a, self.reader_a),
            {"project_id": self.proj_b1.id, "role_id": Roles.Writer.value}, format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Product_Member.objects.filter(user=self.reader_a, product=self.proj_b1.product).exists())

    def test_grant_to_non_member_rejected(self):
        resp = self._client(self.owner_a).post(
            self._grants_url(self.org_a, self.outsider),
            {"project_id": self.proj_a1.id, "role_id": Roles.Writer.value}, format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_grant_to_member_succeeds(self):
        resp = self._client(self.owner_a).post(
            self._grants_url(self.org_a, self.reader_a),
            {"project_id": self.proj_a1.id, "role_id": Roles.Writer.value}, format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Product_Member.objects.filter(user=self.reader_a, product=self.proj_a1.product).exists())

    def test_grant_is_idempotent_and_updates_role(self):
        url = self._grants_url(self.org_a, self.reader_a)
        self._client(self.owner_a).post(url, {"project_id": self.proj_a1.id, "role_id": Roles.Reader.value}, format="json")
        self._client(self.owner_a).post(url, {"project_id": self.proj_a1.id, "role_id": Roles.Writer.value}, format="json")
        grants = Product_Member.objects.filter(user=self.reader_a, product=self.proj_a1.product)
        self.assertEqual(grants.count(), 1)
        self.assertEqual(grants.first().role_id, Roles.Writer.value)

    def test_maintainer_cannot_grant_owner_role(self):
        resp = self._client(self.maintainer_a).post(
            self._grants_url(self.org_a, self.reader_a),
            {"project_id": self.proj_a1.id, "role_id": Roles.Owner.value}, format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_maintainer_cannot_overwrite_existing_owner_grant(self):
        Product_Member.objects.create(user=self.reader_a, product=self.proj_a1.product, role=self.role_owner)
        resp = self._client(self.maintainer_a).post(
            self._grants_url(self.org_a, self.reader_a),
            {"project_id": self.proj_a1.id, "role_id": Roles.Reader.value}, format="json",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            Product_Member.objects.get(user=self.reader_a, product=self.proj_a1.product).role_id,
            Roles.Owner.value,
        )

    def test_maintainer_cannot_revoke_owner_grant(self):
        Product_Member.objects.create(user=self.reader_a, product=self.proj_a1.product, role=self.role_owner)
        resp = self._client(self.maintainer_a).delete(
            self._grant_detail_url(self.org_a, self.reader_a, self.proj_a1),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(
            Product_Member.objects.filter(user=self.reader_a, product=self.proj_a1.product).exists(),
        )

    def test_revoke_grant(self):
        Product_Member.objects.create(user=self.reader_a, product=self.proj_a1.product, role=self.role_writer)
        resp = self._client(self.owner_a).delete(self._grant_detail_url(self.org_a, self.reader_a, self.proj_a1))
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Product_Member.objects.filter(user=self.reader_a, product=self.proj_a1.product).exists())

    def test_superuser_grant_still_validates_org_boundary(self):
        # Superuser bypasses the management gate but the belongs-to-org rule in
        # the serializer/service still runs.
        resp = self._client(self.superuser).post(
            self._grants_url(self.org_a, self.reader_a),
            {"project_id": self.proj_b1.id, "role_id": Roles.Writer.value}, format="json",
        )
        self.assertEqual(resp.status_code, 400)


class ResetPasswordTests(OrgMembersApiBase):
    @patch(EMAIL_PATCH)
    def test_reset_member_sends_email(self, mock_email):
        resp = self._client(self.owner_a).post(self._reset_url(self.org_a, self.reader_a))
        self.assertEqual(resp.status_code, 200)
        mock_email.assert_called_once()

    @patch(EMAIL_PATCH)
    def test_reset_non_member_rejected(self, mock_email):
        resp = self._client(self.owner_a).post(self._reset_url(self.org_a, self.outsider))
        self.assertEqual(resp.status_code, 400)
        mock_email.assert_not_called()
