"""
Tests for scoped AIST API tokens: authentication, scope enforcement, lifecycle,
self-service endpoints (IDOR-safe), and the superuser overview.

The org-isolation half is covered by test_project_access_scoping; here we verify
that token auth resolves to the correct user (so that scoping applies) and that a
token can only narrow capability, never widen it.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.http import JsonResponse
from django.test import TestCase
from django.test.client import RequestFactory
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from aist.models import AISTApiToken, ApiTokenScope
from aist_site.middleware import AistResponseMaskingMiddleware, AistTokenScopeMiddleware

User = get_user_model()


class TokenTestBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pass")
        self.other = User.objects.create_user("bob", "bob@example.com", "pass")
        self.superuser = User.objects.create_superuser("root", "root@example.com", "pass")

    def _bearer(self, raw: str) -> APIClient:
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        return client

    def _session(self, user) -> APIClient:
        client = APIClient()
        client.force_authenticate(user=user)
        return client


class TokenAuthenticationTests(TokenTestBase):
    def test_valid_token_authenticates_on_aist_api(self):
        _t, raw = AISTApiToken.issue(user=self.user, name="t", scope=ApiTokenScope.READ_ONLY)
        resp = self._bearer(raw).get(reverse("aist_api:me"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["username"], "alice")

    def test_invalid_secret_rejected(self):
        _t, raw = AISTApiToken.issue(user=self.user, name="t", scope=ApiTokenScope.READ_ONLY)
        tampered = raw[:-4] + "xxxx"
        resp = self._bearer(tampered).get(reverse("aist_api:me"))
        self.assertEqual(resp.status_code, 403)

    def test_revoked_token_rejected(self):
        token, raw = AISTApiToken.issue(user=self.user, name="t", scope=ApiTokenScope.READ_ONLY)
        token.delete()
        resp = self._bearer(raw).get(reverse("aist_api:me"))
        self.assertEqual(resp.status_code, 403)

    def test_expired_token_rejected(self):
        _t, raw = AISTApiToken.issue(
            user=self.user, name="t", scope=ApiTokenScope.READ_ONLY,
            expires_at=timezone.now() - timedelta(hours=1),
        )
        resp = self._bearer(raw).get(reverse("aist_api:me"))
        self.assertEqual(resp.status_code, 403)

    def test_inactive_user_token_rejected(self):
        _t, raw = AISTApiToken.issue(user=self.user, name="t", scope=ApiTokenScope.READ_ONLY)
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
        _t, raw = AISTApiToken.issue(user=self.user, name="ro", scope=ApiTokenScope.READ_ONLY)
        resp = self._bearer(raw).get(reverse("aist_api:me_token_list_create"))
        self.assertEqual(resp.status_code, 200)

    def test_read_only_token_blocks_write(self):
        _t, raw = AISTApiToken.issue(user=self.user, name="ro", scope=ApiTokenScope.READ_ONLY)
        resp = self._bearer(raw).post(
            reverse("aist_api:me_token_list_create"),
            {"name": "another", "scope": ApiTokenScope.READ_ONLY}, format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_read_write_token_allows_write(self):
        _t, raw = AISTApiToken.issue(user=self.user, name="rw", scope=ApiTokenScope.READ_WRITE)
        resp = self._bearer(raw).post(
            reverse("aist_api:me_token_list_create"),
            {"name": "another", "scope": ApiTokenScope.READ_ONLY}, format="json",
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
        _t, raw = AISTApiToken.issue(user=self.user, name="ro", scope=ApiTokenScope.READ_ONLY)
        url = reverse("aist_api:finding_export", kwargs={"finding_id": 1})
        response = self._run_scope_middleware("POST", url, raw)
        self.assertEqual(response.status_code, 200)

    def test_read_only_token_blocked_on_normal_write(self):
        _t, raw = AISTApiToken.issue(user=self.user, name="ro2", scope=ApiTokenScope.READ_ONLY)
        url = reverse("aist_api:me_token_list_create")
        response = self._run_scope_middleware("POST", url, raw)
        self.assertEqual(response.status_code, 403)

    def test_read_write_token_allowed_on_normal_write(self):
        _t, raw = AISTApiToken.issue(user=self.user, name="rw", scope=ApiTokenScope.READ_WRITE)
        url = reverse("aist_api:me_token_list_create")
        response = self._run_scope_middleware("POST", url, raw)
        self.assertEqual(response.status_code, 200)


class TokenSelfServiceTests(TokenTestBase):
    def test_create_returns_secret_once(self):
        resp = self._session(self.user).post(
            reverse("aist_api:me_token_list_create"),
            {"name": "ci", "scope": ApiTokenScope.READ_WRITE}, format="json",
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertTrue(body["token"].startswith("aistpat_"))
        self.assertEqual(body["scope"], ApiTokenScope.READ_WRITE)

    def test_list_never_exposes_secret(self):
        AISTApiToken.issue(user=self.user, name="t", scope=ApiTokenScope.READ_ONLY)
        resp = self._session(self.user).get(reverse("aist_api:me_token_list_create"))
        self.assertEqual(resp.status_code, 200)
        for item in resp.json():
            self.assertNotIn("token", item)
            self.assertNotIn("secret_hash", item)
            self.assertNotIn("public_id", item)

    def test_duplicate_name_rejected(self):
        AISTApiToken.issue(user=self.user, name="dup", scope=ApiTokenScope.READ_ONLY)
        resp = self._session(self.user).post(
            reverse("aist_api:me_token_list_create"),
            {"name": "dup", "scope": ApiTokenScope.READ_ONLY}, format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_same_name_allowed_across_users(self):
        AISTApiToken.issue(user=self.user, name="shared", scope=ApiTokenScope.READ_ONLY)
        resp = self._session(self.other).post(
            reverse("aist_api:me_token_list_create"),
            {"name": "shared", "scope": ApiTokenScope.READ_ONLY}, format="json",
        )
        self.assertEqual(resp.status_code, 201)

    def test_past_expiry_rejected(self):
        resp = self._session(self.user).post(
            reverse("aist_api:me_token_list_create"),
            {"name": "t", "scope": ApiTokenScope.READ_ONLY,
             "expires_at": (timezone.now() - timedelta(days=1)).isoformat()},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_cannot_delete_another_users_token(self):
        token, _raw = AISTApiToken.issue(user=self.user, name="mine", scope=ApiTokenScope.READ_ONLY)
        resp = self._session(self.other).delete(
            reverse("aist_api:me_token_detail", kwargs={"token_id": token.id}),
        )
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(AISTApiToken.objects.filter(pk=token.id).exists())

    def test_delete_own_token(self):
        token, _raw = AISTApiToken.issue(user=self.user, name="mine", scope=ApiTokenScope.READ_ONLY)
        resp = self._session(self.user).delete(
            reverse("aist_api:me_token_detail", kwargs={"token_id": token.id}),
        )
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(AISTApiToken.objects.filter(pk=token.id).exists())

    def test_unauthenticated_cannot_list(self):
        resp = APIClient().get(reverse("aist_api:me_token_list_create"))
        self.assertEqual(resp.status_code, 403)


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

    def test_create_opts_out_so_secret_survives(self):
        def view(_request):
            response = JsonResponse({"token": "aistpat_abc_secret"})
            response.aist_disable_masking = True
            return response
        request = self.factory.get("/aist/api/v2/aist/me/tokens/")
        response = AistResponseMaskingMiddleware(view)(request)
        self.assertIn("aistpat_abc_secret", response.content.decode())


class TokenStorageTests(TokenTestBase):
    def test_secret_is_not_stored_in_plaintext(self):
        _t, raw = AISTApiToken.issue(user=self.user, name="t", scope=ApiTokenScope.READ_ONLY)
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
        AISTApiToken.issue(user=self.user, name="ci", scope=ApiTokenScope.READ_WRITE)
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
        _t, raw = AISTApiToken.issue(user=self.superuser, name="t", scope=ApiTokenScope.READ_WRITE)
        resp = self._bearer(raw).get(self._url())
        self.assertIn(resp.status_code, (401, 403))
