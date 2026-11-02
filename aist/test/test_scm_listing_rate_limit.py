"""
How often this installation may be made to call a tenant's own SCM host.

Listing an organization's repositories hands the request to a Celery task and then waits
on its result for up to 350 seconds, so a web worker is held for the whole of an outbound
call that leaves through the VPN sidecar. The import dialog now lists as soon as an
organization is chosen, which makes the call cheap to repeat -- a held key on the select,
or a loop in a script, is enough. The bound is keyed by organization because that is whose
integration, and whose host, the traffic lands on.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from django.core.cache import caches
from django.test import Client
from django.urls import reverse
from rest_framework.throttling import SimpleRateThrottle

from aist.models import Organization, OrgIntegration, OrgIntegrationType
from aist.test.test_api import AISTApiBase
from aist.utils.cache import QUEUE_CACHE_ALIAS
from aist.views.throttling import SCM_PROJECT_LISTING_SCOPE

#: Each dialog, with the task it dispatches and the integration it needs.
PROVIDERS = {
    "gitlab": ("aist:gitlab_projects_list", "fetch_gitlab_projects", OrgIntegrationType.GITLAB),
    "gerrit": ("aist:gerrit_projects_list", "fetch_gerrit_projects", OrgIntegrationType.GERRIT),
    "gitea": ("aist:gitea_projects_list", "fetch_gitea_projects", OrgIntegrationType.GITEA),
}

ALLOWED_PER_HOUR = 2


class ScmListingRateLimitTests(AISTApiBase):

    """Rate lowered to two listings per organization per hour."""

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.client = Client()
        self.client.force_login(self.user)

        # The counters live in the shared store, which `cache.clear()` does not touch.
        caches[QUEUE_CACHE_ALIAS].clear()
        # THROTTLE_RATES is bound to the settings dict at import, so override_settings
        # never reaches the throttle.
        rates = patch.dict(
            SimpleRateThrottle.THROTTLE_RATES,
            {SCM_PROJECT_LISTING_SCOPE: f"{ALLOWED_PER_HOUR}/hour"},
        )
        rates.start()
        self.addCleanup(rates.stop)

        self.organization = Organization.objects.create(name="Listing org")
        self.other_organization = Organization.objects.create(name="Other listing org")
        for org in (self.organization, self.other_organization):
            for provider, (_url, _task, integration_type) in PROVIDERS.items():
                OrgIntegration.objects.create(
                    organization=org,
                    integration_type=integration_type,
                    name=f"{provider} for {org.name}",
                    config={"base_url": "https://scm.example.com"},
                    secret="not-a-real-token",  # noqa: S106
                    is_active=True,
                )

    def _list(self, provider, organization, organization_id=None):
        url, task_name, _type = PROVIDERS[provider]
        if organization_id is None:
            organization_id = organization.pk
        with patch(f"aist.views.integrations.{task_name}") as task:
            task.delay.return_value.get.return_value = {"ok": True, "projects": []}
            response = self.client.post(
                reverse(url), data={"organization_id": organization_id})
        return response, task

    def test_a_loop_of_listings_is_cut_off_before_it_becomes_traffic(self):
        for attempt in range(ALLOWED_PER_HOUR):
            allowed, _task = self._list("gitlab", self.organization)
            self.assertEqual(allowed.status_code, 200, f"listing {attempt} was refused")

        refused, task = self._list("gitlab", self.organization)

        self.assertEqual(refused.status_code, 429, refused.content)
        task.delay.assert_not_called()

    def test_a_refusal_says_what_happened_and_when_to_come_back(self):
        """The dialog shows `error` in its own box; a page with no message reads as broken."""
        for _attempt in range(ALLOWED_PER_HOUR):
            self._list("gitlab", self.organization)

        refused, _task = self._list("gitlab", self.organization)

        payload = json.loads(refused.content.decode("utf-8"))
        self.assertFalse(payload["ok"])
        self.assertIn("organization", payload["error"].lower())
        self.assertGreater(int(refused["Retry-After"]), 0)

    def test_one_organizations_budget_is_not_spent_on_another(self):
        for _attempt in range(ALLOWED_PER_HOUR + 1):
            self._list("gitlab", self.organization)

        allowed, _task = self._list("gitlab", self.other_organization)

        self.assertEqual(allowed.status_code, 200, allowed.content)

    def test_changing_provider_does_not_refill_the_budget(self):
        """Three dialogs, three hosts, one organization paying for all of them."""
        self._list("gitlab", self.organization)
        self._list("gerrit", self.organization)

        refused, task = self._list("gitea", self.organization)

        self.assertEqual(refused.status_code, 429, refused.content)
        task.delay.assert_not_called()

    def test_respelling_the_organization_id_does_not_buy_a_fresh_budget(self):
        """
        The budget belongs to the organization, not to how its id was written. `05` and
        `+5` reach the same host as `5`, so a caller cycling spellings would otherwise
        list without any bound at all.
        """
        self._list("gitlab", self.organization)
        self._list("gitlab", self.organization, organization_id=f"0{self.organization.pk}")

        refused, task = self._list(
            "gitlab", self.organization, organization_id=f"+{self.organization.pk}")

        self.assertEqual(refused.status_code, 429, refused.content)
        task.delay.assert_not_called()

    def test_every_listing_dialog_is_bounded(self):
        """A provider added later without the guard is the gap this closes."""
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                caches[QUEUE_CACHE_ALIAS].clear()
                organization = Organization.objects.create(name=f"Org for {provider}")
                OrgIntegration.objects.create(
                    organization=organization,
                    integration_type=PROVIDERS[provider][2],
                    name=provider,
                    config={"base_url": "https://scm.example.com"},
                    secret="not-a-real-token",  # noqa: S106
                    is_active=True,
                )
                for _attempt in range(ALLOWED_PER_HOUR):
                    self._list(provider, organization)

                refused, _task = self._list(provider, organization)

                self.assertEqual(refused.status_code, 429, refused.content)
