"""
Tests for scoped AIST API tokens: authentication, scope enforcement, lifecycle,
self-service endpoints (IDOR-safe), and the superuser overview.

The org-isolation half is covered by test_project_access_scoping; here we verify
that token auth resolves to the correct user (so that scoping applies) and that a
token can only narrow capability, never widen it.
"""
from __future__ import annotations

import threading
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.http import JsonResponse
from django.test import Client, TestCase, TransactionTestCase
from django.test.client import RequestFactory
from django.urls import reverse
from django.utils import timezone
from dojo.authorization.roles_permissions import Roles
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
from rest_framework.test import APIClient

from aist import api_urls
from aist.models import AISTApiToken, AISTProject, ApiTokenScope, LaunchSchedule, Organization, OrgMemberAccessScope
from aist.utils.secrets import view_disables_masking
from aist_site.middleware import AistResponseMaskingMiddleware, AistTokenScopeMiddleware

User = get_user_model()


class TokenTestBase(TestCase):
    def setUp(self):
        # A test earlier in the same run may have exhausted the aist_auth_login
        # ScopedRateThrottle (10/min default) — its cache persists across test
        # classes/modules within one run, so a real session-login test here
        # (TokenCreateRealSessionTests) could otherwise see a spurious 429.
        cache.clear()
        self.user = User.objects.create_user("alice", "alice@example.com", "pass")
        self.other = User.objects.create_user("bob", "bob@example.com", "pass")
        self.superuser = User.objects.create_superuser("root", "root@example.com", "pass")
        token_product_type = Product_Type.objects.create(name=f"Token org PT {self.id()}")
        self.token_org = Organization.objects.create(name=f"Token org {self.id()}", product_type=token_product_type)
        Product.objects.create(
            name=f"Token org product {self.id()}",
            description="d",
            prod_type=token_product_type,
        )
        reader_role, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        Product_Type_Member.objects.create(product_type=token_product_type, user=self.user, role=reader_role)
        Product_Type_Member.objects.create(product_type=token_product_type, user=self.other, role=reader_role)

    def _issue(self, **kwargs):
        kwargs.setdefault("organization", self.token_org)
        return AISTApiToken.issue(**kwargs)

    def _bearer(self, raw: str) -> APIClient:
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        return client

    def _session(self, user) -> APIClient:
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _grant_write_role(self, user, *, role=Roles.Writer) -> Organization:
        """
        Give ``user`` a real Writer-or-above role on a fresh product, so
        scope=read_write token creation (gated by
        aist.queries.user_has_write_capability) succeeds for them.
        """
        pt = Product_Type.objects.create(name=f"Write-capable PT {user.pk}")
        organization = Organization.objects.create(name=f"Write-capable Org {user.pk}", product_type=pt)
        sla = SLA_Configuration.objects.create(name=f"SLA {user.pk}")
        Product.objects.create(
            name=f"Write-capable Product {user.pk}", description="d", prod_type=pt, sla_configuration_id=sla.id,
        )
        role_obj, _ = Role.objects.get_or_create(id=role, defaults={"name": role.name})
        Product_Type_Member.objects.create(product_type=pt, user=user, role=role_obj)
        return organization


class TokenAuthenticationTests(TokenTestBase):
    def test_valid_token_authenticates_on_aist_api(self):
        _t, raw = self._issue(user=self.user, name="t", scope=ApiTokenScope.READ_ONLY)
        resp = self._bearer(raw).get(reverse("aist_api:me"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["username"], "alice")

    def test_invalid_secret_rejected(self):
        _t, raw = self._issue(user=self.user, name="t", scope=ApiTokenScope.READ_ONLY)
        tampered = raw[:-4] + "xxxx"
        resp = self._bearer(tampered).get(reverse("aist_api:me"))
        self.assertEqual(resp.status_code, 403)

    def test_revoked_token_rejected(self):
        token, raw = self._issue(user=self.user, name="t", scope=ApiTokenScope.READ_ONLY)
        token.delete()
        resp = self._bearer(raw).get(reverse("aist_api:me"))
        self.assertEqual(resp.status_code, 403)

    def test_expired_token_rejected(self):
        _t, raw = self._issue(
            user=self.user, name="t", scope=ApiTokenScope.READ_ONLY,
            expires_at=timezone.now() - timedelta(hours=1),
        )
        resp = self._bearer(raw).get(reverse("aist_api:me"))
        self.assertEqual(resp.status_code, 403)

    def test_inactive_user_token_rejected(self):
        _t, raw = self._issue(user=self.user, name="t", scope=ApiTokenScope.READ_ONLY)
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        resp = self._bearer(raw).get(reverse("aist_api:me"))
        self.assertEqual(resp.status_code, 403)

    def test_malformed_bearer_does_not_crash(self):
        for value in ("Bearer aistpat_garbage", "Bearer notours", "Bearer aistpat_"):
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=value)
            resp = client.get(reverse("aist_api:me"))
            self.assertIn(resp.status_code, (401, 403))


class TokenScopeEnforcementTests(TokenTestBase):
    def test_read_only_token_allows_get(self):
        _t, raw = self._issue(user=self.user, name="ro", scope=ApiTokenScope.READ_ONLY)
        resp = self._bearer(raw).get(reverse("aist_api:me_token_list_create"))
        self.assertEqual(resp.status_code, 200)

    def test_read_only_token_blocks_write(self):
        _t, raw = self._issue(user=self.user, name="ro", scope=ApiTokenScope.READ_ONLY)
        resp = self._bearer(raw).post(
            reverse("aist_api:me_token_list_create"),
            {"name": "another", "scope": ApiTokenScope.READ_ONLY, "organization_id": self.token_org.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_read_write_token_allows_write(self):
        _t, raw = self._issue(user=self.user, name="rw", scope=ApiTokenScope.READ_WRITE)
        resp = self._bearer(raw).post(
            reverse("aist_api:me_token_list_create"),
            {"name": "another", "scope": ApiTokenScope.READ_ONLY, "organization_id": self.token_org.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)


class TokenScopeEndpointDeclarationTests(TokenTestBase):

    """Scope enforcement is generic + endpoint-declared (no hardcoded paths)."""

    def _run_scope_middleware(self, method: str, url: str, raw: str):
        factory = RequestFactory()
        request = getattr(factory, method.lower())(url, HTTP_AUTHORIZATION=f"Bearer {raw}")
        middleware = AistTokenScopeMiddleware(lambda _r: JsonResponse({"ok": True}))
        return middleware(request)

    def test_read_only_token_allowed_on_declared_read_post(self):
        # finding export is a POST declared token_read_only=True.
        _t, raw = self._issue(user=self.user, name="ro", scope=ApiTokenScope.READ_ONLY)
        url = reverse("aist_api:finding_export", kwargs={"finding_id": 1})
        response = self._run_scope_middleware("POST", url, raw)
        self.assertEqual(response.status_code, 200)

    def test_read_only_token_blocked_on_normal_write(self):
        _t, raw = self._issue(user=self.user, name="ro2", scope=ApiTokenScope.READ_ONLY)
        url = reverse("aist_api:me_token_list_create")
        response = self._run_scope_middleware("POST", url, raw)
        self.assertEqual(response.status_code, 403)

    def test_read_write_token_allowed_on_normal_write(self):
        _t, raw = self._issue(user=self.user, name="rw", scope=ApiTokenScope.READ_WRITE)
        url = reverse("aist_api:me_token_list_create")
        response = self._run_scope_middleware("POST", url, raw)
        self.assertEqual(response.status_code, 200)


class TokenScopeCapabilityTests(TokenTestBase):

    """
    A user must not be able to mint a read_write token unless they hold a
    Writer-or-above role in the selected organization — enforced centrally by
    ``aist.queries.user_has_write_capability`` (reused, not reimplemented, by
    ``AISTApiTokenCreateSerializer.validate`` and by
    ``AISTMeSerializer.can_create_write_token``). Downstream endpoints already
    re-derive authorization per-request (see TokenDestructiveActionTests /
    TokenCrossResourceDataScopeTests), so this is a defense-in-depth /
    least-privilege guard, not the sole enforcement point.
    """

    def _create(self, user, scope, *, organization=None):
        client = self._session(user)
        organization = organization or self.token_org
        return client.post(
            reverse("aist_api:me_token_list_create"),
            {"name": f"tok-{scope}", "scope": scope, "organization_id": organization.id}, format="json",
        )

    def test_user_with_no_org_membership_cannot_create_read_write_token(self):
        outsider = User.objects.create_user("no-org-rw", "no-org-rw@example.com", "pass")
        resp = self._create(outsider, ApiTokenScope.READ_WRITE)
        self.assertEqual(resp.status_code, 400)

    def test_user_with_no_org_membership_cannot_create_read_only_token(self):
        outsider = User.objects.create_user("no-org-ro", "no-org-ro@example.com", "pass")
        resp = self._create(outsider, ApiTokenScope.READ_ONLY)
        self.assertEqual(resp.status_code, 400)

    def test_full_reader_member_cannot_create_read_write_token(self):
        # A Product must actually exist here — otherwise this would pass for the
        # wrong reason (empty product set) rather than because Reader genuinely
        # doesn't qualify for Finding_Edit.
        sla = SLA_Configuration.objects.create(name="SLA Reader")
        pt = Product_Type.objects.create(name="Reader PT")
        organization = Organization.objects.create(name="Reader Org", product_type=pt)
        Product.objects.create(name="Reader Product", description="d", prod_type=pt, sla_configuration_id=sla.id)
        role_reader, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        Product_Type_Member.objects.create(product_type=pt, user=self.user, role=role_reader)
        resp = self._create(self.user, ApiTokenScope.READ_WRITE, organization=organization)
        self.assertEqual(resp.status_code, 400)

    def test_restricted_member_with_only_a_reader_project_grant_cannot_create_read_write_token(self):
        # Mirrors a real restricted org member: baseline org membership (Reader,
        # never real access on its own) plus a single per-project Product_Member
        # grant that is ALSO Reader — the exact "Reader on one project" shape.
        sla = SLA_Configuration.objects.create(name="SLA Restricted")
        pt = Product_Type.objects.create(name="Restricted PT")
        org = Organization.objects.create(name="Restricted Org", product_type=pt)
        role_reader, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        Product_Type_Member.objects.create(product_type=pt, user=self.user, role=role_reader)
        OrgMemberAccessScope.objects.create(organization=org, user=self.user, restricted=True)
        product = Product.objects.create(
            name="Restricted Product", description="d", prod_type=pt, sla_configuration_id=sla.id,
        )
        Product_Member.objects.create(product=product, user=self.user, role=role_reader)

        resp = self._create(self.user, ApiTokenScope.READ_WRITE, organization=org)
        self.assertEqual(resp.status_code, 400)

    def test_member_with_a_writer_grant_can_create_read_write_token(self):
        organization = self._grant_write_role(self.user)
        resp = self._create(self.user, ApiTokenScope.READ_WRITE, organization=organization)
        self.assertEqual(resp.status_code, 201)

    def test_writer_cannot_bind_read_write_token_to_reader_organization(self):
        self._grant_write_role(self.user)
        resp = self._create(self.user, ApiTokenScope.READ_WRITE, organization=self.token_org)
        self.assertEqual(resp.status_code, 400)

    def test_superuser_can_create_read_write_token_with_no_memberships_at_all(self):
        resp = self._create(self.superuser, ApiTokenScope.READ_WRITE)
        self.assertEqual(resp.status_code, 201)


class TokenSelfServiceTests(TokenTestBase):
    def test_create_returns_secret_once(self):
        # Secret-reveal behavior is what's under test here, not scope gating —
        # scope=read_write needs a real Writer+ grant since
        # aist.queries.user_has_write_capability now enforces it at create time.
        organization = self._grant_write_role(self.user)
        resp = self._session(self.user).post(
            reverse("aist_api:me_token_list_create"),
            {"name": "ci", "scope": ApiTokenScope.READ_WRITE, "organization_id": organization.id}, format="json",
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertTrue(body["token"].startswith("aistpat_"))
        self.assertEqual(body["scope"], ApiTokenScope.READ_WRITE)

    def test_list_never_exposes_secret(self):
        self._issue(user=self.user, name="t", scope=ApiTokenScope.READ_ONLY)
        resp = self._session(self.user).get(reverse("aist_api:me_token_list_create"))
        self.assertEqual(resp.status_code, 200)
        for item in resp.json():
            self.assertNotIn("token", item)
            self.assertNotIn("secret_hash", item)
            self.assertNotIn("public_id", item)

    def test_duplicate_name_rejected(self):
        self._issue(user=self.user, name="dup", scope=ApiTokenScope.READ_ONLY)
        resp = self._session(self.user).post(
            reverse("aist_api:me_token_list_create"),
            {"name": "dup", "scope": ApiTokenScope.READ_ONLY, "organization_id": self.token_org.id}, format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_same_name_allowed_across_users(self):
        self._issue(user=self.user, name="shared", scope=ApiTokenScope.READ_ONLY)
        resp = self._session(self.other).post(
            reverse("aist_api:me_token_list_create"),
            {"name": "shared", "scope": ApiTokenScope.READ_ONLY, "organization_id": self.token_org.id}, format="json",
        )
        self.assertEqual(resp.status_code, 201)

    def test_past_expiry_rejected(self):
        resp = self._session(self.user).post(
            reverse("aist_api:me_token_list_create"),
            {"name": "t", "scope": ApiTokenScope.READ_ONLY,
             "organization_id": self.token_org.id,
             "expires_at": (timezone.now() - timedelta(days=1)).isoformat()},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_cannot_delete_another_users_token(self):
        token, _raw = self._issue(user=self.user, name="mine", scope=ApiTokenScope.READ_ONLY)
        resp = self._session(self.other).delete(
            reverse("aist_api:me_token_detail", kwargs={"token_id": token.id}),
        )
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(AISTApiToken.objects.filter(pk=token.id).exists())

    def test_delete_own_token(self):
        token, _raw = self._issue(user=self.user, name="mine", scope=ApiTokenScope.READ_ONLY)
        resp = self._session(self.user).delete(
            reverse("aist_api:me_token_detail", kwargs={"token_id": token.id}),
        )
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(AISTApiToken.objects.filter(pk=token.id).exists())

    def test_unauthenticated_cannot_list(self):
        resp = APIClient().get(reverse("aist_api:me_token_list_create"))
        self.assertEqual(resp.status_code, 403)


class TokenCreateRealSessionTests(TokenTestBase):

    """
    Every test above authenticates via APIClient.force_authenticate(), which
    bypasses Django's session/CSRF pipeline entirely. This is the only test that
    goes through a real browser-equivalent session-login + CSRF flow — the
    coverage gap that let a reported 500 on POST /api/v2/aist/me/tokens/ ship
    untested against real sessions.
    """

    def test_create_token_via_real_session_and_csrf(self):
        # The real-session/CSRF flow is what's under test here, not scope gating —
        # scope=read_write needs a real Writer+ grant since
        # aist.queries.user_has_write_capability now enforces it at create time.
        organization = self._grant_write_role(self.user)
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("client_login"))
        csrf_token = client.cookies["csrftoken"].value
        login_response = client.post(
            reverse("aist_api:auth_login"),
            data={"username": self.user.username, "password": "pass"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(login_response.status_code, 204)

        csrf_after_login = client.cookies["csrftoken"].value
        response = client.post(
            reverse("aist_api:me_token_list_create"),
            data={"name": "ci-real-session", "scope": ApiTokenScope.READ_WRITE, "organization_id": organization.id},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_after_login,
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["token"].startswith("aistpat_"))
        self.assertEqual(body["scope"], ApiTokenScope.READ_WRITE)


class TokenMaskingTests(TestCase):

    """The one-time secret reveal must survive the response-masking middleware."""

    def setUp(self):
        self.factory = RequestFactory()

    def test_token_field_is_masked_by_default_under_aist_prefix(self):
        def view(_request):
            return JsonResponse({"token": "aistpat_abc_secret"})
        request = self.factory.get("/aist/api/v2/aist/me/tokens/")
        response = AistResponseMaskingMiddleware(view)(request)
        self.assertNotIn("aistpat_abc_secret", response.content.decode())

    def test_view_disables_masking_reflects_real_view_declaration(self):
        # AISTMeTokenListCreateAPI declares disable_response_masking = True (the
        # one-time secret reveal); AISTAdminApiTokenListAPI does not. Both masking
        # layers (AistResponseMaskingMiddleware and aist.api.response's
        # finalize_response patch) consult this same shared function, so a
        # regression here breaks masking opt-out everywhere at once. (The actual
        # end-to-end reveal is covered by TokenSelfServiceTests.test_create_returns_secret_once.)
        create_request = self.factory.post(reverse("aist_api:me_token_list_create"))
        self.assertTrue(view_disables_masking(create_request))

        admin_request = self.factory.get(reverse("aist_api:admin_api_token_list"))
        self.assertFalse(view_disables_masking(admin_request))


class TokenStorageTests(TokenTestBase):
    def test_secret_is_not_stored_in_plaintext(self):
        _t, raw = self._issue(user=self.user, name="t", scope=ApiTokenScope.READ_ONLY)
        secret = raw.rsplit("_", 1)[-1]
        token = AISTApiToken.objects.get(name="t")
        # Stored as a Django password hash (not the plaintext secret).
        self.assertNotEqual(token.secret_hash, secret)
        self.assertNotIn(secret, token.secret_hash)
        self.assertTrue(token.verify_secret(secret))
        self.assertFalse(token.verify_secret("wrong"))


class AdminTokenOverviewTests(TokenTestBase):
    def _url(self):
        return reverse("aist_api:admin_api_token_list")

    def test_superuser_sees_users_and_metadata_no_secret(self):
        self._issue(user=self.user, name="ci", scope=ApiTokenScope.READ_WRITE)
        resp = self._session(self.superuser).get(self._url())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        entry = next(e for e in body if e["username"] == "alice")
        self.assertEqual(entry["token_count"], 1)
        token_meta = entry["tokens"][0]
        self.assertEqual(token_meta["scope"], ApiTokenScope.READ_WRITE)
        self.assertNotIn("token", token_meta)
        self.assertNotIn("secret_hash", token_meta)
        self.assertNotIn("public_id", token_meta)

    def test_non_superuser_forbidden(self):
        resp = self._session(self.user).get(self._url())
        self.assertEqual(resp.status_code, 403)

    def test_scoped_token_cannot_reach_admin_overview(self):
        # Even a superuser's scoped token must not enumerate everyone: the view
        # excludes ScopedTokenAuthentication, so the token does not authenticate.
        _t, raw = self._issue(user=self.superuser, name="t", scope=ApiTokenScope.READ_WRITE)
        resp = self._bearer(raw).get(self._url())
        self.assertIn(resp.status_code, (401, 403))


class MultiOrgTokenTests(TokenTestBase):

    """
    A token authenticates a user inside exactly one organization. Membership
    in a second organization must not widen the token's tenant boundary.
    """

    def setUp(self):
        super().setUp()
        self.sla = SLA_Configuration.objects.create(name="SLA")
        self.role_owner, _ = Role.objects.get_or_create(id=Roles.Owner, defaults={"name": "Owner"})
        self.role_reader, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})

        self.pt_a = Product_Type.objects.create(name="Org A")
        self.org_a = Organization.objects.create(name="Org A", product_type=self.pt_a)
        Product.objects.create(name="A1", description="d", prod_type=self.pt_a, sla_configuration_id=self.sla.id)

        self.pt_b = Product_Type.objects.create(name="Org B")
        self.org_b = Organization.objects.create(name="Org B", product_type=self.pt_b)
        Product.objects.create(name="B1", description="d", prod_type=self.pt_b, sla_configuration_id=self.sla.id)

        # self.user (from TokenTestBase): Owner of org_a, Reader of org_b.
        Product_Type_Member.objects.create(product_type=self.pt_a, user=self.user, role=self.role_owner)
        Product_Type_Member.objects.create(product_type=self.pt_b, user=self.user, role=self.role_reader)

    def test_single_token_manages_org_a_but_not_org_b(self):
        _t, raw = self._issue(
            user=self.user, organization=self.org_a, name="ci", scope=ApiTokenScope.READ_WRITE,
        )
        client = self._bearer(raw)
        manage_a = client.get(reverse("aist_api:org_member_list_create", kwargs={"org_id": self.org_a.id}))
        manage_b = client.get(reverse("aist_api:org_member_list_create", kwargs={"org_id": self.org_b.id}))
        # Owner of org_a -> can manage members there.
        self.assertEqual(manage_a.status_code, 200)
        # Org B is outside this token even though the owning user is a member.
        self.assertEqual(manage_b.status_code, 404)

    def test_single_token_only_sees_bound_organization(self):
        _t, raw = self._issue(
            user=self.user, organization=self.org_a, name="ci", scope=ApiTokenScope.READ_ONLY,
        )
        resp = self._bearer(raw).get(reverse("aist_api:me"))
        self.assertEqual(resp.status_code, 200)
        org_names = {m["organization_name"] for m in resp.json()["organization_memberships"]}
        self.assertEqual(org_names, {"Org A"})


class TokenReadOnlyDeclarationHonestyTests(TokenTestBase):

    """
    An attacker holding only a read-only token must not be able to use an
    endpoint declared token_read_only=True as a side channel for a real
    mutation. Verifies the two current declared-read-only POST endpoints are
    genuinely side-effect-free, not merely permitted by the scope middleware
    on trust.
    """

    def test_finding_export_with_read_only_token_produces_no_side_effect(self):
        sla = SLA_Configuration.objects.create(name="SLA RO")
        pt = Product_Type.objects.create(name="RO PT")
        organization = Organization.objects.create(name="RO Org", product_type=pt)
        role_reader, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        Product_Type_Member.objects.create(product_type=pt, user=self.user, role=role_reader)
        product = Product.objects.create(
            name="RO Product", description="d", prod_type=pt, sla_configuration_id=sla.id,
        )
        engagement = Engagement.objects.create(
            name="RO Engagement", target_start=timezone.now(), target_end=timezone.now(), product=product,
        )
        test_type = Test_Type.objects.create(name="RO Test Type")
        test = Test.objects.create(
            engagement=engagement, target_start=timezone.now(), target_end=timezone.now(), test_type=test_type,
        )
        finding = Finding.objects.create(
            test=test, title="RO Finding", severity="High", date=timezone.now(), reporter=self.user,
        )
        before_count = Finding.objects.count()
        before_reviewed = finding.last_reviewed

        _t, raw = self._issue(
            user=self.user, organization=organization, name="ro", scope=ApiTokenScope.READ_ONLY,
        )
        resp = self._bearer(raw).post(reverse("aist_api:finding_export", kwargs={"finding_id": finding.id}), data={})

        self.assertEqual(resp.status_code, 200)
        finding.refresh_from_db()
        self.assertEqual(Finding.objects.count(), before_count, "export must not create records")
        self.assertEqual(finding.last_reviewed, before_reviewed, "export must not mutate the finding it reads")

    def test_launch_schedule_preview_with_read_only_token_persists_nothing(self):
        _t, raw = self._issue(user=self.user, name="ro2", scope=ApiTokenScope.READ_ONLY)
        resp = self._bearer(raw).post(
            reverse("aist_api:launch_schedule_preview"),
            {"cron_expression": "*/5 * * * *", "count": 3}, format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LaunchSchedule.objects.count(), 0, "preview must not persist a LaunchSchedule")


class TokenCreationLimitTests(TokenTestBase):
    def test_no_limit_on_tokens_per_user(self):
        # Known gap, not fixed in this pass: there is no cap on how many
        # tokens a single user may hold, only the per-(user, name) unique
        # constraint. A compromised session (or the token-creation UI itself,
        # see ProjectAccessEditor's unrelated fan-out bug found in the
        # frontend pass) could mint an unbounded number of live credentials.
        # Documents current behavior.
        statuses = [
            self._session(self.user).post(
                reverse("aist_api:me_token_list_create"),
                {"name": f"tok-{i}", "scope": ApiTokenScope.READ_ONLY, "organization_id": self.token_org.id},
                format="json",
            ).status_code
            for i in range(30)
        ]
        self.assertTrue(all(code == 201 for code in statuses), "documents: no per-user token cap exists yet")
        self.assertEqual(AISTApiToken.objects.filter(user=self.user).count(), 30)


class TokenNameRaceTests(TransactionTestCase):

    """Real-thread concurrency regression for the duplicate-name create race."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("race_token_user", "race_token_user@example.com", "pass")
        product_type = Product_Type.objects.create(name="Race token PT")
        self.organization = Organization.objects.create(name="Race token Org", product_type=product_type)
        Product.objects.create(name="Race token product", description="d", prod_type=product_type)
        reader_role, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        Product_Type_Member.objects.create(product_type=product_type, user=self.user, role=reader_role)

    def test_concurrent_create_with_same_name_never_500s(self):
        barrier = threading.Barrier(2)
        results = {}

        def create(key):
            barrier.wait(timeout=5)
            client = APIClient()
            client.force_authenticate(user=self.user)
            resp = client.post(
                reverse("aist_api:me_token_list_create"),
                {"name": "race-token", "scope": ApiTokenScope.READ_ONLY, "organization_id": self.organization.id},
                format="json",
            )
            results[key] = resp.status_code

        t1 = threading.Thread(target=create, args=("a",))
        t2 = threading.Thread(target=create, args=("b",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        statuses = sorted([results.get("a"), results.get("b")])
        self.assertEqual(statuses, [201, 400], "one create must win, the loser must get 400, never a 500")
        self.assertEqual(AISTApiToken.objects.filter(user=self.user, name="race-token").count(), 1)


def _dummy_kwargs_for(url_pattern) -> dict:
    """Synthesize plausible path kwargs from a path()'s converters (int -> 1, else 'x')."""
    kwargs = {}
    for name, converter in url_pattern.pattern.converters.items():
        kwargs[name] = 1 if type(converter).__name__ == "IntConverter" else "x"
    return kwargs


class TokenSystemicScopeInvariantTests(TokenTestBase):

    """
    Enumerates EVERY path registered in aist.api_urls and, for each view that
    handles a mutating HTTP method (post/put/patch/delete) without declaring
    ``token_read_only = True``, asserts a read-only token is rejected by
    AistTokenScopeMiddleware. This fails loudly the moment a future endpoint is
    added without considering token scope, instead of relying on someone
    remembering to add a per-endpoint spot-check.

    A 403 here is a middleware-level guarantee (the view never even runs) — it
    does not by itself prove the view would have been destructive if reached;
    TokenDestructiveActionTests below adds real end-to-end checks for the
    highest-value destructive actions as defense-in-depth on top of this.
    """

    MUTATING_METHODS = ("post", "put", "patch", "delete")

    def _run_scope_middleware(self, method: str, url: str, raw: str):
        factory = RequestFactory()
        request = getattr(factory, method.lower())(url, HTTP_AUTHORIZATION=f"Bearer {raw}")
        middleware = AistTokenScopeMiddleware(lambda _r: JsonResponse({"ok": True}))
        return middleware(request)

    def test_every_undeclared_mutating_endpoint_blocks_read_only_token(self):
        _t, raw = self._issue(user=self.user, name="sweep-ro", scope=ApiTokenScope.READ_ONLY)

        checked = []
        for url_pattern in api_urls.urlpatterns:
            view_class = url_pattern.callback.view_class
            if getattr(view_class, "token_read_only", False):
                continue
            url = reverse(f"aist_api:{url_pattern.name}", kwargs=_dummy_kwargs_for(url_pattern))
            for method in self.MUTATING_METHODS:
                # APIView's own base class defines no get/post/etc — hasattr is a
                # reliable "does this view (or a mixin it uses) handle this method" check.
                if not hasattr(view_class, method):
                    continue
                response = self._run_scope_middleware(method, url, raw)
                checked.append((url_pattern.name, method))
                self.assertEqual(
                    response.status_code, 403,
                    f"{url_pattern.name}.{method} accepts a read-only token without declaring "
                    "token_read_only=True — either mark it read-only or this is a real scope gap",
                )
        # Sanity: the sweep must have actually exercised a meaningful number of
        # endpoints, or a bug in the enumeration itself would silently pass empty.
        self.assertGreater(len(checked), 20, "the sweep found suspiciously few mutating endpoints — check enumeration logic")


class TokenDestructiveActionTests(TokenTestBase):

    """
    Real end-to-end (APIClient + real fixtures) checks that a read-only token
    cannot perform high-value destructive actions, on top of the structural
    middleware-level guarantee in TokenSystemicScopeInvariantTests.
    """

    def setUp(self):
        super().setUp()
        self.sla = SLA_Configuration.objects.create(name="Destructive SLA")
        self.role_owner, _ = Role.objects.get_or_create(id=Roles.Owner, defaults={"name": "Owner"})
        self.role_reader, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        self.pt = Product_Type.objects.create(name="Destructive PT")
        self.org = Organization.objects.create(name="Destructive Org", product_type=self.pt)
        Product_Type_Member.objects.create(product_type=self.pt, user=self.user, role=self.role_owner)
        self.target = User.objects.create_user("victim", "victim@example.com", "pass")
        Product_Type_Member.objects.create(product_type=self.pt, user=self.target, role=self.role_reader)
        self.product = Product.objects.create(
            name="Destructive Product", description="d", prod_type=self.pt, sla_configuration_id=self.sla.id,
        )

    def _ro_token(self) -> str:
        _t, raw = self._issue(
            user=self.user, organization=self.org, name="ro-destructive", scope=ApiTokenScope.READ_ONLY,
        )
        return raw

    def test_read_only_token_cannot_remove_org_member(self):
        resp = self._bearer(self._ro_token()).delete(
            reverse("aist_api:org_member_detail", kwargs={"org_id": self.org.id, "user_id": self.target.id}),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(
            Product_Type_Member.objects.filter(product_type=self.pt, user=self.target).exists(),
            "member must not actually be removed",
        )

    def test_read_only_token_cannot_revoke_project_grant(self):
        project = AISTProject.objects.create(
            product=self.product, supported_languages=["python"], compilable=False, profile={},
        )
        Product_Member.objects.create(product=self.product, user=self.target, role=self.role_reader)
        resp = self._bearer(self._ro_token()).delete(
            reverse(
                "aist_api:org_member_project_grant_detail",
                kwargs={"org_id": self.org.id, "user_id": self.target.id, "project_id": project.id},
            ),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Product_Member.objects.filter(product=self.product, user=self.target).exists())

    def test_read_only_token_cannot_delete_its_own_token(self):
        other_token, _other_raw = self._issue(
            user=self.user, organization=self.org, name="deletable", scope=ApiTokenScope.READ_WRITE,
        )
        resp = self._bearer(self._ro_token()).delete(
            reverse("aist_api:me_token_detail", kwargs={"token_id": other_token.id}),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(AISTApiToken.objects.filter(pk=other_token.id).exists())

    def test_read_only_token_cannot_clear_dispatched_launch_requests(self):
        resp = self._bearer(self._ro_token()).post(
            reverse("aist_api:pipeline_launch_request_clear_dispatched"), {"older_than_days": 0}, format="json",
        )
        self.assertEqual(resp.status_code, 403)


class TokenCrossResourceDataScopeTests(TokenTestBase):

    """
    A token must never see more data than a real session for the same user —
    extends MultiOrgTokenTests (org-member management) to a different resource
    type (findings) to confirm org-scoping via aist.queries applies identically
    regardless of auth method.
    """

    def setUp(self):
        super().setUp()
        self.sla = SLA_Configuration.objects.create(name="Scope SLA")
        self.role_owner, _ = Role.objects.get_or_create(id=Roles.Owner, defaults={"name": "Owner"})

        self.pt_a = Product_Type.objects.create(name="Findings Org A")
        self.org_a = Organization.objects.create(name="Findings Org A", product_type=self.pt_a)
        Product_Type_Member.objects.create(product_type=self.pt_a, user=self.user, role=self.role_owner)
        product_a = Product.objects.create(
            name="FA", description="d", prod_type=self.pt_a, sla_configuration_id=self.sla.id,
        )
        self._create_finding(product_a, "Finding in A")

        # self.other belongs to no org at all -> must see nothing either way.
        self.pt_b = Product_Type.objects.create(name="Findings Org B")
        product_b = Product.objects.create(
            name="FB", description="d", prod_type=self.pt_b, sla_configuration_id=self.sla.id,
        )
        self._create_finding(product_b, "Finding in B")

    def _create_finding(self, product, title):
        engagement = Engagement.objects.create(
            name=f"Eng {title}", target_start=timezone.now(), target_end=timezone.now(), product=product,
        )
        test_type = Test_Type.objects.create(name=f"Type {title}")
        test = Test.objects.create(
            engagement=engagement, target_start=timezone.now(), target_end=timezone.now(), test_type=test_type,
        )
        return Finding.objects.create(test=test, title=title, severity="High", date=timezone.now(), reporter=self.user)

    def test_token_and_session_see_identical_finding_titles(self):
        _t, raw = self._issue(
            user=self.user, organization=self.org_a, name="scope-check", scope=ApiTokenScope.READ_ONLY,
        )
        token_titles = {f["title"] for f in self._bearer(raw).get(reverse("aist_api:finding_list")).json()["results"]}
        session_titles = {
            f["title"] for f in self._session(self.user).get(reverse("aist_api:finding_list")).json()["results"]
        }
        self.assertEqual(token_titles, session_titles)
        self.assertIn("Finding in A", token_titles)
        self.assertNotIn("Finding in B", token_titles, "user has no membership in Org B -> must not see its findings")
