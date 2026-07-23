"""
API tests for organization member & per-project access management.

The matrix deliberately concentrates on ACCESS-VIOLATION scenarios: cross-org
grants, granting to non-members, management by non-managers, Owner-role
escalation, the last-owner guard, and idempotency under concurrent edits.
Email delivery is mocked — these tests assert the membership state machine, not
SMTP.
"""
from __future__ import annotations

import threading
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from dojo.authorization.roles_permissions import Permissions, Roles
from dojo.models import (
    Product,
    Product_Member,
    Product_Type,
    Product_Type_Member,
    Role,
    SLA_Configuration,
)
from rest_framework.test import APIClient

from aist.members.service import OrganizationMembershipService
from aist.models import (
    AISTApiToken,
    AISTProject,
    ApiTokenScope,
    Organization,
    OrgMemberAccessScope,
    OrgMembershipAction,
    OrgMembershipHistory,
    ProjectAccessDenial,
)
from aist.queries import get_authorized_aist_products

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

    def _reset_access_url(self, org, user):
        return reverse("aist_api:org_member_reset_access", kwargs={"org_id": org.id, "user_id": user.id})

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
        # "restricted" is the explicit OrgMemberAccessScope flag, not implied
        # by grant presence — a bare Product_Member grant alone is now a
        # legitimate per-project downgrade for a full member (see
        # test_full_member_grant_capped_at_org_role).
        Product_Member.objects.create(user=self.reader_a, product=self.proj_a1.product, role=self.role_reader)
        OrgMemberAccessScope.objects.create(organization=self.org_a, user=self.reader_a, restricted=True)
        resp = self._client(self.owner_a).get(self._members_url(self.org_a))
        by_user = {m["user_id"]: m for m in resp.json()}
        self.assertEqual(by_user[self.owner_a.id]["membership_type"], "full")
        self.assertEqual(by_user[self.reader_a.id]["membership_type"], "restricted")
        # A user with no org membership never appears, even with a stray grant.
        self.assertNotIn(self.outsider.id, by_user)

    def test_reader_cannot_list_project_grants(self):
        resp = self._client(self.reader_a).get(self._grants_url(self.org_a, self.owner_a))
        self.assertEqual(resp.status_code, 404)

    def test_reader_cannot_create_project_grant(self):
        resp = self._client(self.reader_a).post(
            self._grants_url(self.org_a, self.reader_a),
            {"project_id": self.proj_a1.id, "role_id": Roles.Writer.value}, format="json",
        )
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(Product_Member.objects.filter(user=self.reader_a, product=self.proj_a1.product).exists())

    def test_reader_cannot_delete_project_grant(self):
        Product_Member.objects.create(user=self.reader_a, product=self.proj_a1.product, role=self.role_writer)
        resp = self._client(self.reader_a).delete(self._grant_detail_url(self.org_a, self.reader_a, self.proj_a1))
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(Product_Member.objects.filter(user=self.reader_a, product=self.proj_a1.product).exists())

    def test_list_reports_token_indicator_without_secret(self):
        AISTApiToken.issue(
            user=self.reader_a, organization=self.org_a, name="ci", scope=ApiTokenScope.READ_ONLY,
        )
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
        self.assertEqual(resp.json()["invite_status"], "invited")
        user = User.objects.get(email="new@example.com")
        self.assertTrue(user.is_active)
        self.assertTrue(user.has_usable_password())
        self.assertTrue(
            Product_Type_Member.objects.filter(product_type=self.pt_a, user=user).exists(),
        )
        mock_email.assert_called_once()

    @patch(EMAIL_PATCH)
    def test_invite_existing_user_does_not_email_or_duplicate(self, mock_email):
        resp = self._client(self.owner_a).post(
            self._members_url(self.org_a),
            {"email": self.owner_b.email, "role_id": Roles.Reader.value},
            format="json",
        )
        # Distinguishes "existing user added to a new-to-them org" (no email,
        # they already have working credentials) from a real invite — without
        # this field the frontend can't tell this silent-add apart from a
        # fresh invite and would show a misleading generic "success".
        self.assertEqual(resp.json()["invite_status"], "existing_user_added_no_email")
        self.assertEqual(
            Product_Type_Member.objects.filter(product_type=self.pt_a, user=self.owner_b).count(), 1,
        )
        mock_email.assert_not_called()

    @patch(EMAIL_PATCH)
    def test_reinvite_of_removed_member_sends_email(self, mock_email):
        # Add a brand-new member to org_a, remove them (hard-deletes their
        # Product_Type_Member row — no trace left there), then invite the same
        # email again. Per product decision this is treated exactly like a
        # fresh invite: a new set-password email, not a silent no-op.
        #
        # Mechanism: remove_member deactivates the account once it has no
        # membership left in ANY org (see _deactivate_if_orphaned) — otherwise
        # the removed user would look identical to owner_b above (existing,
        # active user, just with no current membership in org_a) and
        # invite_member would silently skip the email.
        client = self._client(self.owner_a)
        first = client.post(
            self._members_url(self.org_a),
            {"email": "returning@example.com", "role_id": Roles.Reader.value},
            format="json",
        )
        self.assertEqual(first.json()["invite_status"], "invited")
        user = User.objects.get(email="returning@example.com")
        old_password_hash = user.password
        mock_email.assert_called_once()

        client.delete(self._member_url(self.org_a, user))
        self.assertFalse(
            Product_Type_Member.objects.filter(product_type=self.pt_a, user=user).exists(),
        )
        user.refresh_from_db()
        self.assertFalse(user.is_active, "orphaned user (no org left) must be deactivated")

        mock_email.reset_mock()
        second = client.post(
            self._members_url(self.org_a),
            {"email": "returning@example.com", "role_id": Roles.Reader.value},
            format="json",
        )
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()["invite_status"], "invited")
        mock_email.assert_called_once()
        self.assertTrue(
            Product_Type_Member.objects.filter(product_type=self.pt_a, user=user).exists(),
        )
        user.refresh_from_db()
        self.assertTrue(user.is_active, "re-invited user must be reactivated")
        self.assertNotEqual(user.password, old_password_hash, "must force a fresh set-password, not reuse the old one")

    def test_removal_does_not_deactivate_orphaned_superuser(self):
        # Superuser access doesn't derive from org membership — losing their
        # last org membership must never lock them out of everything else.
        Product_Type_Member.objects.create(product_type=self.pt_a, user=self.superuser, role=self.role_owner)
        self._client(self.owner_a).delete(self._member_url(self.org_a, self.superuser))
        self.superuser.refresh_from_db()
        self.assertTrue(self.superuser.is_active)

    @patch(EMAIL_PATCH)
    def test_removal_does_not_deactivate_member_of_another_org(self, mock_email):
        # owner_b belongs to org_b. Removing them from org_a (where they were
        # never a member — nothing to remove) must never touch is_active; more
        # importantly, a user removed from ONE org they belong to, while still
        # active in another, must stay active — losing access to org_a alone
        # must not lock them out of org_b.
        Product_Type_Member.objects.create(product_type=self.pt_a, user=self.owner_b, role=self.role_reader)
        self._client(self.owner_a).delete(self._member_url(self.org_a, self.owner_b))
        self.owner_b.refresh_from_db()
        self.assertTrue(self.owner_b.is_active)
        self.assertTrue(
            Product_Type_Member.objects.filter(product_type=self.pt_b, user=self.owner_b).exists(),
        )

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

    @patch(EMAIL_PATCH)
    def test_invite_ignores_privileged_extra_fields(self, mock_email):
        # AISTMemberInviteSerializer has no field for is_superuser/organization/
        # product_type — DRF silently drops unknown input keys, but that
        # contract deserves an explicit regression test given how security
        # sensitive this endpoint is.
        resp = self._client(self.owner_a).post(
            self._members_url(self.org_a),
            {
                "email": "massassign@example.com",
                "role_id": Roles.Reader.value,
                "is_superuser": True,
                "organization": self.org_b.id,
                "product_type": self.pt_b.id,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        user = User.objects.get(email="massassign@example.com")
        self.assertFalse(user.is_superuser)
        self.assertTrue(Product_Type_Member.objects.filter(product_type=self.pt_a, user=user).exists())
        self.assertFalse(Product_Type_Member.objects.filter(product_type=self.pt_b, user=user).exists())

    @patch(EMAIL_PATCH)
    def test_invite_status_leaks_platform_wide_email_existence(self, mock_email):
        # Characterizes an accepted-but-risky design point (see
        # invite_member's own docstring on INVITE_OUTCOME_*): a Manage_Members
        # holder can use invite_status to probe whether ANY email on the whole
        # platform already has an account — not just within their org — and,
        # if it does, silently add that unrelated user into their org with a
        # chosen role and zero consent. This is not a bug being fixed here
        # (it's the documented intended behavior for "existing user of
        # another org"); this test makes the oracle observable so a future
        # decision to restrict it has something to change.
        never_seen_before = self._client(self.owner_a).post(
            self._members_url(self.org_a),
            {"email": "definitely-new@example.com", "role_id": Roles.Reader.value},
            format="json",
        )
        already_exists_elsewhere = self._client(self.owner_a).post(
            self._members_url(self.org_a),
            {"email": self.owner_b.email, "role_id": Roles.Reader.value},
            format="json",
        )
        self.assertEqual(never_seen_before.json()["invite_status"], "invited")
        self.assertEqual(already_exists_elsewhere.json()["invite_status"], "existing_user_added_no_email")
        # owner_b (a member of org_b only, with no relationship to org_a or
        # its owner) is now silently a member of org_a too, with no action on
        # their part.
        self.assertTrue(Product_Type_Member.objects.filter(product_type=self.pt_a, user=self.owner_b).exists())

    @patch(EMAIL_PATCH)
    def test_invite_endpoint_is_rate_limited(self, mock_email):
        cache.clear()
        client = self._client(self.owner_a)
        statuses = [
            client.post(
                self._members_url(self.org_a),
                {"email": f"bulk-{i}@example.com", "role_id": Roles.Reader.value},
                format="json",
            ).status_code
            for i in range(25)
        ]
        self.assertIn(429, statuses)
        self.assertLess(mock_email.call_count, 25)

    @patch(EMAIL_PATCH)
    def test_api_importer_role_cannot_be_assigned_on_invite(self, mock_email):
        resp = self._client(self.owner_a).post(
            self._members_url(self.org_a),
            {"email": "api-importer@example.com", "role_id": Roles.API_Importer.value},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(User.objects.filter(email="api-importer@example.com").exists())
        mock_email.assert_not_called()


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

    def test_owner_cannot_change_own_role_even_when_another_owner_exists(self):
        self._member("owner_a2", self.pt_a, self.role_owner)
        resp = self._client(self.owner_a).patch(
            self._member_url(self.org_a, self.owner_a),
            {"role_id": Roles.Maintainer.value}, format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            Product_Type_Member.objects.get(product_type=self.pt_a, user=self.owner_a).role_id,
            Roles.Owner.value,
        )

    def test_api_importer_role_cannot_be_assigned_to_member(self):
        resp = self._client(self.owner_a).patch(
            self._member_url(self.org_a, self.reader_a),
            {"role_id": Roles.API_Importer.value}, format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            Product_Type_Member.objects.get(product_type=self.pt_a, user=self.reader_a).role_id,
            Roles.Reader.value,
        )

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

    def test_change_role_ignores_privileged_extra_fields(self):
        # AISTMemberRoleSerializer only has role_id — extra keys like
        # is_superuser must be silently dropped, never interpreted.
        resp = self._client(self.owner_a).patch(
            self._member_url(self.org_a, self.reader_a),
            {"role_id": Roles.Writer.value, "is_superuser": True}, format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.reader_a.refresh_from_db()
        self.assertFalse(self.reader_a.is_superuser)


class RemoveMemberTests(OrgMembersApiBase):
    def test_remove_full_member_clears_all_access(self):
        Product_Member.objects.create(user=self.reader_a, product=self.proj_a1.product, role=self.role_writer)
        ProjectAccessDenial.objects.create(user=self.reader_a, project=self.proj_a2)
        token, _raw = AISTApiToken.issue(
            user=self.reader_a,
            organization=self.org_a,
            name="org-a-token",
            scope=ApiTokenScope.READ_ONLY,
        )
        resp = self._client(self.owner_a).delete(self._member_url(self.org_a, self.reader_a))
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Product_Type_Member.objects.filter(product_type=self.pt_a, user=self.reader_a).exists())
        self.assertFalse(Product_Member.objects.filter(user=self.reader_a, product__prod_type=self.pt_a).exists())
        self.assertFalse(ProjectAccessDenial.objects.filter(user=self.reader_a, project=self.proj_a2).exists())
        token.refresh_from_db()
        self.assertIsNotNone(token.revoked_at)

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
        # Grant is above reader_a's org-wide Reader role, so — per the new
        # "project role can only be ≤ org role" cap for full members — this
        # scenario only makes sense for a restricted (allow-list) member.
        OrgMemberAccessScope.objects.create(organization=self.org_a, user=self.reader_a, restricted=True)
        resp = self._client(self.owner_a).post(
            self._grants_url(self.org_a, self.reader_a),
            {"project_id": self.proj_a1.id, "role_id": Roles.Writer.value}, format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Product_Member.objects.filter(user=self.reader_a, product=self.proj_a1.product).exists())

    def test_grant_is_idempotent_and_updates_role(self):
        OrgMemberAccessScope.objects.create(organization=self.org_a, user=self.reader_a, restricted=True)
        url = self._grants_url(self.org_a, self.reader_a)
        self._client(self.owner_a).post(url, {"project_id": self.proj_a1.id, "role_id": Roles.Reader.value}, format="json")
        self._client(self.owner_a).post(url, {"project_id": self.proj_a1.id, "role_id": Roles.Writer.value}, format="json")
        grants = Product_Member.objects.filter(user=self.reader_a, product=self.proj_a1.product)
        self.assertEqual(grants.count(), 1)
        self.assertEqual(grants.first().role_id, Roles.Writer.value)

    def test_maintainer_cannot_grant_owner_role(self):
        # Restricted, so the cap doesn't pre-empt the Add_Owner permission
        # check this test actually targets.
        OrgMemberAccessScope.objects.create(organization=self.org_a, user=self.reader_a, restricted=True)
        resp = self._client(self.maintainer_a).post(
            self._grants_url(self.org_a, self.reader_a),
            {"project_id": self.proj_a1.id, "role_id": Roles.Owner.value}, format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_full_member_grant_capped_at_org_role(self):
        # New rule: a full member's per-project role can never exceed their
        # org-wide role — granting higher is a 400, not a silent elevation.
        resp = self._client(self.owner_a).post(
            self._grants_url(self.org_a, self.reader_a),
            {"project_id": self.proj_a1.id, "role_id": Roles.Writer.value}, format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Product_Member.objects.filter(user=self.reader_a, product=self.proj_a1.product).exists())

    def test_restricted_member_grant_exempt_from_cap(self):
        # Restricted members have no meaningful org-wide role to cap against
        # — grants are their only real access, any role is legitimate.
        OrgMemberAccessScope.objects.create(organization=self.org_a, user=self.reader_a, restricted=True)
        resp = self._client(self.owner_a).post(
            self._grants_url(self.org_a, self.reader_a),
            {"project_id": self.proj_a1.id, "role_id": Roles.Owner.value}, format="json",
        )
        self.assertEqual(resp.status_code, 200)

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

    def test_revoking_every_project_from_a_full_member_leaves_them_with_no_access(self):
        # THE regression test for the original reported bug: reader_a starts
        # as a full member (org role, zero project grants). Setting every org
        # project to "No access" in the UI fires a revoke call per project —
        # each one used to be a no-op against Product_Member (there was never
        # a row to delete), so membership silently stayed unaffected and the
        # member kept access to every project at their org-wide role. Now
        # each revoke records an explicit per-project denial instead — the
        # member stays "full" (denying every project individually must NOT
        # narrow them to allow-list mode; see the one-project variant below),
        # but ends up with zero actual access since every project is denied.
        client = self._client(self.owner_a)
        for project in (self.proj_a1, self.proj_a2):
            resp = client.delete(self._grant_detail_url(self.org_a, self.reader_a, project))
            self.assertEqual(resp.status_code, 204)

        members = {m["user_id"]: m for m in client.get(self._members_url(self.org_a)).json()}
        self.assertEqual(members[self.reader_a.id]["membership_type"], "full")
        self.assertEqual(members[self.reader_a.id]["project_grants"], [])
        self.assertEqual(
            set(members[self.reader_a.id]["denied_project_ids"]), {self.proj_a1.id, self.proj_a2.id},
        )

        authorized = get_authorized_aist_products(Permissions.Product_View, user=self.reader_a)
        self.assertFalse(authorized.filter(prod_type=self.pt_a).exists())

    def test_revoking_one_project_from_a_full_member_does_not_affect_others(self):
        # The bug reported after the previous fix: denying ONE project must
        # leave every other, untouched project visible at the org-wide role.
        client = self._client(self.owner_a)
        resp = client.delete(self._grant_detail_url(self.org_a, self.reader_a, self.proj_a1))
        self.assertEqual(resp.status_code, 204)

        members = {m["user_id"]: m for m in client.get(self._members_url(self.org_a)).json()}
        self.assertEqual(members[self.reader_a.id]["membership_type"], "full")
        self.assertEqual(members[self.reader_a.id]["denied_project_ids"], [self.proj_a1.id])

        authorized = get_authorized_aist_products(Permissions.Product_View, user=self.reader_a)
        self.assertFalse(authorized.filter(pk=self.proj_a1.product_id).exists())
        self.assertTrue(authorized.filter(pk=self.proj_a2.product_id).exists())


class ResetAccessTests(OrgMembersApiBase):
    def test_reset_to_full_access_clears_restriction_and_grants(self):
        Product_Member.objects.create(user=self.reader_a, product=self.proj_a1.product, role=self.role_writer)
        OrgMemberAccessScope.objects.create(organization=self.org_a, user=self.reader_a, restricted=True)

        resp = self._client(self.owner_a).post(self._reset_access_url(self.org_a, self.reader_a))
        self.assertEqual(resp.status_code, 200)

        self.assertFalse(Product_Member.objects.filter(user=self.reader_a, product__prod_type=self.pt_a).exists())
        members = {m["user_id"]: m for m in self._client(self.owner_a).get(self._members_url(self.org_a)).json()}
        self.assertEqual(members[self.reader_a.id]["membership_type"], "full")

        authorized = get_authorized_aist_products(Permissions.Product_View, user=self.reader_a)
        self.assertTrue(authorized.filter(pk=self.proj_a1.product_id).exists())
        self.assertTrue(authorized.filter(pk=self.proj_a2.product_id).exists())

    def test_reset_access_rejected_for_non_manager(self):
        resp = self._client(self.reader_a).post(self._reset_access_url(self.org_a, self.reader_a))
        self.assertEqual(resp.status_code, 404)

    def test_reset_access_for_non_member_rejected(self):
        resp = self._client(self.owner_a).post(self._reset_access_url(self.org_a, self.outsider))
        self.assertEqual(resp.status_code, 400)


class InviteRestrictedMemberScopeTests(OrgMembersApiBase):
    @patch(EMAIL_PATCH)
    def test_invite_restricted_member_persists_restricted_flag(self, mock_email):
        resp = self._client(self.owner_a).post(
            self._members_url(self.org_a),
            {
                "email": "new.restricted@example.com",
                "project_grants": [{"project_id": self.proj_a1.id, "role_id": Roles.Writer.value}],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        user_id = resp.json()["user_id"]
        self.assertTrue(
            OrgMemberAccessScope.objects.get(organization=self.org_a, user_id=user_id).restricted,
        )

        # Revoking the only grant must leave them with zero access, not fall
        # back to full org access, exactly like the reader_a regression above.
        new_user = User.objects.get(pk=user_id)
        self._client(self.owner_a).delete(self._grant_detail_url(self.org_a, new_user, self.proj_a1))
        authorized = get_authorized_aist_products(Permissions.Product_View, user=new_user)
        self.assertFalse(authorized.filter(prod_type=self.pt_a).exists())


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

    @patch(EMAIL_PATCH)
    def test_reset_password_endpoint_has_no_rate_limit(self, mock_email):
        # Known gap, not fixed in this pass: any Manage_Members holder can
        # mail-bomb a specific member's inbox with reset-password emails with
        # no cooldown/throttle. Documents current behavior.
        client = self._client(self.owner_a)
        statuses = [client.post(self._reset_url(self.org_a, self.reader_a)).status_code for _ in range(15)]
        self.assertNotIn(429, statuses, "documents: reset-password endpoint is not yet throttled")
        self.assertEqual(mock_email.call_count, 15)

    def test_invite_email_failure_rolls_back_membership_creation(self):
        # send_set_password_email runs synchronously INSIDE invite_member's
        # @transaction.atomic block, so a transient SMTP failure rolls back
        # the whole invite rather than leaving a member with no way to log
        # in — but it also means a slow/unavailable SMTP server holds the row
        # locks taken during this invite for as long as the send takes,
        # which is the DoS-relevant half of this same design point.
        with patch(EMAIL_PATCH, side_effect=RuntimeError("smtp down")):
            response = self._client(self.owner_a).post(
                self._members_url(self.org_a),
                {"email": "rollback@example.com", "role_id": Roles.Reader.value},
                format="json",
            )
        self.assertGreaterEqual(response.status_code, 500)
        self.assertFalse(User.objects.filter(email="rollback@example.com").exists())
        self.assertFalse(
            Product_Type_Member.objects.filter(product_type=self.pt_a, user__email="rollback@example.com").exists(),
        )


class MultiOrgAdminTests(OrgMembersApiBase):

    """
    Every admin fixture in the classes above (owner_a, owner_b, ...) manages
    exactly ONE org. These tests build a single real admin who manages TWO
    organizations (possibly with a different role in each) and check that
    every mutation stays correctly scoped to the org named in the URL — never
    bleeding into, or being blocked by, the admin's role in the other org.
    """

    def setUp(self):
        super().setUp()
        # owner_a is made an Owner of org_b too — one real admin, two orgs.
        Product_Type_Member.objects.create(product_type=self.pt_b, user=self.owner_a, role=self.role_owner)

    @patch(EMAIL_PATCH)
    def test_admin_of_two_orgs_can_invite_into_each_independently(self, mock_email):
        client = self._client(self.owner_a)
        resp_a = client.post(
            self._members_url(self.org_a),
            {"email": "new-a@example.com", "role_id": Roles.Reader.value}, format="json",
        )
        resp_b = client.post(
            self._members_url(self.org_b),
            {"email": "new-b@example.com", "role_id": Roles.Reader.value}, format="json",
        )
        self.assertEqual(resp_a.status_code, 201)
        self.assertEqual(resp_b.status_code, 201)
        new_a = User.objects.get(email="new-a@example.com")
        new_b = User.objects.get(email="new-b@example.com")
        self.assertTrue(Product_Type_Member.objects.filter(product_type=self.pt_a, user=new_a).exists())
        self.assertFalse(Product_Type_Member.objects.filter(product_type=self.pt_b, user=new_a).exists())
        self.assertTrue(Product_Type_Member.objects.filter(product_type=self.pt_b, user=new_b).exists())
        self.assertFalse(Product_Type_Member.objects.filter(product_type=self.pt_a, user=new_b).exists())

    def test_role_change_in_one_org_does_not_touch_the_other_org(self):
        # owner_b is Owner of org_b only; add them to org_a as a plain Reader
        # too, so ONE user now has two different roles in two orgs.
        Product_Type_Member.objects.create(product_type=self.pt_a, user=self.owner_b, role=self.role_reader)

        client = self._client(self.owner_a)
        resp = client.patch(
            self._member_url(self.org_a, self.owner_b),
            {"role_id": Roles.Writer.value}, format="json",
        )
        self.assertEqual(resp.status_code, 200)

        org_a_membership = Product_Type_Member.objects.get(product_type=self.pt_a, user=self.owner_b)
        org_b_membership = Product_Type_Member.objects.get(product_type=self.pt_b, user=self.owner_b)
        self.assertEqual(org_a_membership.role_id, Roles.Writer.value)
        self.assertEqual(org_b_membership.role_id, Roles.Owner.value, "org_b's role must be untouched")

    def test_owner_of_org_a_only_still_cannot_manage_org_b(self):
        # Sanity check the inverse: being Owner of org_a (and ONLY org_a, this
        # time using the unmodified maintainer_a fixture) must not leak into
        # org_b management, matching test_owner_cannot_manage_other_org above
        # but re-asserted here alongside the genuinely-multi-org admin tests.
        resp = self._client(self.maintainer_a).get(self._members_url(self.org_b))
        self.assertEqual(resp.status_code, 404)

    @patch(EMAIL_PATCH)
    def test_removing_member_from_org_a_leaves_their_org_b_membership_intact(self, mock_email):
        # reader_a is a plain member of org_a; also add them to org_b.
        Product_Type_Member.objects.create(product_type=self.pt_b, user=self.reader_a, role=self.role_reader)

        resp = self._client(self.owner_a).delete(self._member_url(self.org_a, self.reader_a))
        self.assertEqual(resp.status_code, 204)

        self.assertFalse(Product_Type_Member.objects.filter(product_type=self.pt_a, user=self.reader_a).exists())
        self.assertTrue(Product_Type_Member.objects.filter(product_type=self.pt_b, user=self.reader_a).exists())
        self.reader_a.refresh_from_db()
        self.assertTrue(self.reader_a.is_active, "still a member of org_b — must not be deactivated")


class LastOwnerRaceTests(TransactionTestCase):

    """
    Real-thread concurrency regression for OrganizationMembershipService's
    last-owner guard (_guard_last_owner). Uses TransactionTestCase (not
    TestCase) because the two removal requests need real, independently
    committing transactions on separate DB connections — TestCase's wrapping
    transaction would prevent that.
    """

    def setUp(self):
        self.role_owner, _ = Role.objects.get_or_create(id=Roles.Owner, defaults={"name": "Owner"})
        self.pt = Product_Type.objects.create(name="Race Org")
        self.org = Organization.objects.create(name="Race Org", product_type=self.pt)
        self.owner1 = User.objects.create_user("race_owner1", "race_owner1@example.com", "pass")
        self.owner2 = User.objects.create_user("race_owner2", "race_owner2@example.com", "pass")
        Product_Type_Member.objects.create(product_type=self.pt, user=self.owner1, role=self.role_owner)
        Product_Type_Member.objects.create(product_type=self.pt, user=self.owner2, role=self.role_owner)

    def test_concurrent_removal_of_last_two_owners_leaves_at_least_one(self):
        owner1_id = self.owner1.id
        owner2_id = self.owner2.id
        ready = threading.Event()
        release = threading.Event()
        original_guard = OrganizationMembershipService._guard_last_owner

        def synchronized_guard(self, *, excluding_user_id):
            # Runs the REAL guard (real select_for_update lock against real
            # Postgres) first, then — only for the thread removing owner1 —
            # holds the still-open transaction open until the test signals,
            # forcing thread2's own select_for_update to genuinely block on
            # the row lock instead of racing to read stale state.
            original_guard(self, excluding_user_id=excluding_user_id)
            if excluding_user_id == owner1_id:
                ready.set()
                release.wait(timeout=5)

        results = {}

        def remove(user_id, actor, key):
            client = APIClient()
            client.force_authenticate(user=actor)
            url = reverse("aist_api:org_member_detail", kwargs={"org_id": self.org.id, "user_id": user_id})
            results[key] = client.delete(url).status_code

        with patch.object(OrganizationMembershipService, "_guard_last_owner", synchronized_guard):
            t1 = threading.Thread(target=remove, args=(owner1_id, self.owner1, "t1"))
            t1.start()
            self.assertTrue(ready.wait(timeout=5), "t1 did not reach the guard in time")

            t2 = threading.Thread(target=remove, args=(owner2_id, self.owner2, "t2"))
            t2.start()
            t2.join(timeout=2)
            self.assertTrue(t2.is_alive(), "t2 should still be blocked on the row lock held by t1")

            release.set()
            t1.join(timeout=5)
            t2.join(timeout=5)

        statuses = sorted([results.get("t1"), results.get("t2")])
        # Exactly one removal succeeds; the other is correctly rejected once
        # it sees the post-commit state — never both succeeding, which would
        # leave the organization with zero Owners.
        self.assertEqual(statuses, [204, 400])
        remaining_owners = Product_Type_Member.objects.filter(
            product_type=self.pt, role_id=Roles.Owner.value,
        ).count()
        self.assertGreaterEqual(remaining_owners, 1)


class InviteRaceTests(TransactionTestCase):

    """Real-thread concurrency regression for _get_or_create_user's invite race."""

    def setUp(self):
        self.role_reader, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        self.role_owner, _ = Role.objects.get_or_create(id=Roles.Owner, defaults={"name": "Owner"})
        self.pt_x = Product_Type.objects.create(name="Race Org X")
        self.org_x = Organization.objects.create(name="Race Org X", product_type=self.pt_x)
        self.pt_y = Product_Type.objects.create(name="Race Org Y")
        self.org_y = Organization.objects.create(name="Race Org Y", product_type=self.pt_y)
        self.owner_x = User.objects.create_user("race_owner_x", "race_owner_x@example.com", "pass")
        self.owner_y = User.objects.create_user("race_owner_y", "race_owner_y@example.com", "pass")
        Product_Type_Member.objects.create(product_type=self.pt_x, user=self.owner_x, role=self.role_owner)
        Product_Type_Member.objects.create(product_type=self.pt_y, user=self.owner_y, role=self.role_owner)

    @patch(EMAIL_PATCH)
    def test_concurrent_invites_for_same_new_email_create_one_user(self, mock_email):
        barrier = threading.Barrier(2)
        results = {}

        def invite(org, owner, key):
            barrier.wait(timeout=5)
            client = APIClient()
            client.force_authenticate(user=owner)
            resp = client.post(
                reverse("aist_api:org_member_list_create", kwargs={"org_id": org.id}),
                {"email": "race@example.com", "role_id": Roles.Reader.value},
                format="json",
            )
            results[key] = resp.status_code

        t1 = threading.Thread(target=invite, args=(self.org_x, self.owner_x, "x"))
        t2 = threading.Thread(target=invite, args=(self.org_y, self.owner_y, "y"))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(results.get("x"), 201)
        self.assertEqual(results.get("y"), 201)
        matching_users = User.objects.filter(email__iexact="race@example.com")
        self.assertEqual(matching_users.count(), 1, "concurrent invites for the same new email must not create duplicate users")
        user = matching_users.get()
        self.assertTrue(Product_Type_Member.objects.filter(product_type=self.pt_x, user=user).exists())
        self.assertTrue(Product_Type_Member.objects.filter(product_type=self.pt_y, user=user).exists())


class GrantRevokeRaceTests(TransactionTestCase):

    """
    Real-thread concurrency regression for the select_for_update() lock added
    to grant_project/revoke_project (see _project_in_org_or_400's lock=True
    docstring): concurrent grant + revoke calls on the SAME (user, project)
    pair must never leave both a Product_Member grant and a
    ProjectAccessDenial existing at once.
    """

    def setUp(self):
        self.role_owner, _ = Role.objects.get_or_create(id=Roles.Owner, defaults={"name": "Owner"})
        self.role_writer, _ = Role.objects.get_or_create(id=Roles.Writer, defaults={"name": "Writer"})
        self.sla = SLA_Configuration.objects.create(name="SLA-race-grant")
        self.pt = Product_Type.objects.create(name="Race Grant Org")
        self.org = Organization.objects.create(name="Race Grant Org", product_type=self.pt)
        product = Product.objects.create(
            name="RaceProj", description="d", prod_type=self.pt, sla_configuration_id=self.sla.id,
        )
        self.project = AISTProject.objects.create(
            product=product, supported_languages=["python"], compilable=False, profile={},
        )
        self.actor = User.objects.create_user("race_grant_actor", "race_grant_actor@example.com", "pass")
        self.target = User.objects.create_user("race_grant_target", "race_grant_target@example.com", "pass")
        # Owner org role for the target so the new "project role capped at
        # org role" rule never rejects the Writer grant this test performs —
        # concurrency is what's under test here, not the cap.
        Product_Type_Member.objects.create(product_type=self.pt, user=self.actor, role=self.role_owner)
        Product_Type_Member.objects.create(product_type=self.pt, user=self.target, role=self.role_owner)

    def test_concurrent_grant_and_revoke_never_leave_both_grant_and_denial(self):
        ready = threading.Event()
        release = threading.Event()
        pause_lock = threading.Lock()
        paused_once = {"value": False}
        original_lookup = OrganizationMembershipService._project_in_org_or_400

        def synchronized_lookup(self, project_id, *, lock=False):
            project = original_lookup(self, project_id, lock=lock)
            if lock:
                with pause_lock:
                    should_pause = not paused_once["value"]
                    paused_once["value"] = True
                if should_pause:
                    # Holds the real Postgres row lock open (still inside the
                    # caller's @transaction.atomic) until the test releases
                    # it, forcing the concurrent call to genuinely block
                    # instead of racing to read stale, pre-commit state.
                    ready.set()
                    release.wait(timeout=5)
            return project

        results = {}

        def grant():
            client = APIClient()
            client.force_authenticate(user=self.actor)
            url = reverse(
                "aist_api:org_member_project_grant_list_create",
                kwargs={"org_id": self.org.id, "user_id": self.target.id},
            )
            results["grant"] = client.post(
                url, {"project_id": self.project.id, "role_id": Roles.Writer.value}, format="json",
            ).status_code

        def revoke():
            client = APIClient()
            client.force_authenticate(user=self.actor)
            url = reverse(
                "aist_api:org_member_project_grant_detail",
                kwargs={"org_id": self.org.id, "user_id": self.target.id, "project_id": self.project.id},
            )
            results["revoke"] = client.delete(url).status_code

        with patch.object(OrganizationMembershipService, "_project_in_org_or_400", synchronized_lookup):
            t1 = threading.Thread(target=grant)
            t1.start()
            self.assertTrue(ready.wait(timeout=5), "grant did not reach the lock in time")

            t2 = threading.Thread(target=revoke)
            t2.start()
            t2.join(timeout=2)
            self.assertTrue(t2.is_alive(), "revoke should still be blocked on the row lock held by grant")

            release.set()
            t1.join(timeout=5)
            t2.join(timeout=5)

        self.assertEqual(results.get("grant"), 200)
        self.assertEqual(results.get("revoke"), 204)
        # Never both: revoke ran strictly after grant committed (forced by
        # the lock), so it correctly deleted the grant and recorded a denial.
        has_grant = Product_Member.objects.filter(user=self.target, product=self.project.product).exists()
        has_denial = ProjectAccessDenial.objects.filter(user=self.target, project=self.project).exists()
        self.assertFalse(has_grant and has_denial, "a project must never end up both granted and denied at once")
        self.assertTrue(has_denial)
        self.assertFalse(has_grant)


class SqlInjectionSafetyTests(OrgMembersApiBase):

    """
    _get_or_create_user's advisory-lock call (service.py) is the only raw SQL
    introduced by the user-management rework; it's already parameterized
    (cursor.execute(sql, [param]), never string-formatted). These tests prove
    that in practice, both through the real endpoint (validator-constrained
    input) and by calling the service directly with a value validation would
    normally reject (proving the SQL layer itself is safe independent of what
    validates above it).
    """

    @patch(EMAIL_PATCH)
    def test_invite_with_quote_in_local_part_via_real_endpoint(self, mock_email):
        # Django's EmailValidator permits a single quote in the local part
        # (RFC 5322 dot-atom-text) — this is a realistic, validator-accepted
        # value that would break naive string-formatted SQL.
        email = "a'b@example.com"
        resp = self._client(self.owner_a).post(
            self._members_url(self.org_a),
            {"email": email, "role_id": Roles.Reader.value},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(User.objects.filter(email__iexact=email).exists())

    def test_get_or_create_user_survives_sql_metacharacters_bypassing_validation(self):
        service = OrganizationMembershipService(self.org_a, self.owner_a)
        malicious = "a'; DROP TABLE aist_apitoken; --@example.com"
        with patch(EMAIL_PATCH):
            user, created = service._get_or_create_user(malicious, "First", "Last")
        self.assertTrue(created)
        # The aist_apitoken table must still exist and be queryable — proves no
        # injection occurred via the parameterized advisory-lock call.
        self.assertEqual(AISTApiToken.objects.count(), 0)
        self.assertTrue(User.objects.filter(pk=user.pk).exists())


class RateLimitTests(OrgMembersApiBase):

    """
    Invite/reset-password emails are actor-authenticated, so ScopedRateThrottle
    keys by the actor's user id — these bound how many emails one admin can fire,
    independent of any other scope.
    """

    def setUp(self):
        super().setUp()
        cache.clear()

    @patch(EMAIL_PATCH)
    def test_invite_email_is_throttled_per_actor(self, mock_email):
        client = self._client(self.owner_a)
        statuses = [
            client.post(
                self._members_url(self.org_a),
                {"email": f"spam{i}@example.com", "role_id": Roles.Reader.value},
                format="json",
            ).status_code
            for i in range(25)
        ]
        self.assertIn(429, statuses)

    def test_reset_password_email_is_throttled_per_actor(self):
        client = self._client(self.owner_a)
        with patch(EMAIL_PATCH):
            statuses = [
                client.post(self._reset_url(self.org_a, self.reader_a)).status_code
                for _ in range(25)
            ]
        self.assertIn(429, statuses)

    def test_invite_email_list_get_is_not_throttled(self):
        # Only the invite POST sends mail; listing members must stay unlimited.
        client = self._client(self.owner_a)
        statuses = [client.get(self._members_url(self.org_a)).status_code for _ in range(25)]
        self.assertNotIn(429, statuses)

    @patch(EMAIL_PATCH)
    def test_invite_and_reset_password_scopes_are_independent(self, mock_email):
        client = self._client(self.owner_a)
        for i in range(20):
            client.post(
                self._members_url(self.org_a),
                {"email": f"other{i}@example.com", "role_id": Roles.Reader.value},
                format="json",
            )
        exhausted = client.post(
            self._members_url(self.org_a),
            {"email": "final@example.com", "role_id": Roles.Reader.value},
            format="json",
        )
        self.assertEqual(exhausted.status_code, 429)

        reset_resp = client.post(self._reset_url(self.org_a, self.reader_a))
        self.assertNotEqual(
            reset_resp.status_code, 429,
            "invite-email throttle exhaustion must not bleed into reset-password-email scope",
        )


class AuditTrailTests(OrgMembersApiBase):

    """OrgMembershipHistory is written inside the same transaction as each mutation."""

    @patch(EMAIL_PATCH)
    def test_invite_records_history(self, mock_email):
        resp = self._client(self.owner_a).post(
            self._members_url(self.org_a),
            {"email": "audited@example.com", "role_id": Roles.Reader.value},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        user = User.objects.get(email="audited@example.com")
        entry = OrgMembershipHistory.objects.get(target_user=user, organization=self.org_a)
        self.assertEqual(entry.action, OrgMembershipAction.INVITED)
        self.assertEqual(entry.actor, self.owner_a)
        self.assertIsNone(entry.previous_role)
        self.assertEqual(entry.new_role, Roles.Reader.value)

    def test_role_change_records_previous_and_new_role(self):
        resp = self._client(self.owner_a).patch(
            self._member_url(self.org_a, self.reader_a),
            {"role_id": Roles.Writer.value},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        entry = OrgMembershipHistory.objects.get(
            target_user=self.reader_a, organization=self.org_a, action=OrgMembershipAction.ROLE_CHANGED,
        )
        self.assertEqual(entry.actor, self.owner_a)
        self.assertEqual(entry.previous_role, Roles.Reader.value)
        self.assertEqual(entry.new_role, Roles.Writer.value)

    def test_remove_records_role_held_at_removal(self):
        resp = self._client(self.owner_a).delete(self._member_url(self.org_a, self.reader_a))
        self.assertEqual(resp.status_code, 204)
        entry = OrgMembershipHistory.objects.get(
            target_user=self.reader_a, organization=self.org_a, action=OrgMembershipAction.REMOVED,
        )
        self.assertEqual(entry.actor, self.owner_a)
        self.assertEqual(entry.previous_role, Roles.Reader.value)
        self.assertIsNone(entry.new_role)

    @patch(EMAIL_PATCH)
    def test_remove_then_reinvite_creates_two_distinct_rows(self, mock_email):
        client = self._client(self.owner_a)
        client.post(
            self._members_url(self.org_a),
            {"email": "churn@example.com", "role_id": Roles.Reader.value},
            format="json",
        )
        user = User.objects.get(email="churn@example.com")
        client.delete(self._member_url(self.org_a, user))
        client.post(
            self._members_url(self.org_a),
            {"email": "churn@example.com", "role_id": Roles.Reader.value},
            format="json",
        )
        entries = OrgMembershipHistory.objects.filter(target_user=user, organization=self.org_a).order_by("created")
        self.assertEqual(
            list(entries.values_list("action", flat=True)),
            [OrgMembershipAction.INVITED, OrgMembershipAction.REMOVED, OrgMembershipAction.INVITED],
        )
