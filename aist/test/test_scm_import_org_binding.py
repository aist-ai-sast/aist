"""
An SCM import sends the organization the list on screen was fetched for.

Choosing an organization in an import dialog starts a listing without stopping the one
already in flight, so two answers can arrive out of order. If the later-arriving answer for
an organization that is no longer selected were rendered, Import would send that other
organization's repositories under the currently selected one -- projects from one tenant's
SCM host linked to another tenant. The page therefore remembers which organization a list
belongs to, drops an answer that is no longer the selection, and imports against the
remembered organization rather than whatever the select happens to hold.

Checked against the template because this is a browser-side invariant with no server-side
counterpart: the import endpoint authorizes the organization it is given, and the caller is
authorized for both, so nothing downstream can tell the two apart.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.test import SimpleTestCase

_PROJECTS = Path(__file__).resolve().parent.parent / "templates" / "aist" / "projects.html"

#: The dialogs that list a tenant's repositories before importing them.
PROVIDERS = ("gitlab", "gerrit", "gitea")


class ScmImportOrganizationBindingTests(SimpleTestCase):
    def setUp(self):
        self.page = _PROJECTS.read_text(encoding="utf-8")

    def test_a_listing_records_the_organization_it_was_asked_for(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                self.assertRegex(
                    self.page,
                    rf"var requestedOrg = {provider}OrgSelect \? \({provider}OrgSelect\.value",
                )
                self.assertIn(f"{provider}ListedOrg = requestedOrg;", self.page)

    def test_an_answer_for_another_organization_is_dropped(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                self.assertRegex(
                    self.page,
                    rf"if \({provider}OrgSelect && \({provider}OrgSelect\.value \|\| \"\"\) "
                    rf"!== requestedOrg\) \{{\s*return;",
                )

    def test_import_sends_the_organization_the_list_belongs_to(self):
        """Reading the live select for the import is the bug this guards against."""
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                self.assertIn(f'var org = {provider}ListedOrg || "";', self.page)
                self.assertNotIn(
                    f"var org = {provider}OrgSelect", self.page,
                    "an import still reads the current selection rather than the list's organization")

    def test_clearing_the_list_clears_what_it_belonged_to(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                cleared = re.search(
                    rf"function clear{provider.capitalize()}State\(\) \{{(.*?)\n            \}}",
                    self.page, re.DOTALL)
                self.assertIsNotNone(cleared, "clear helper not found")
                self.assertIn(f'{provider}ListedOrg = "";', cleared.group(1))

    def test_a_listing_that_failed_binds_nothing(self):
        """
        A refusal -- the rate limit, a dead host -- leaves no list on screen, so there is
        nothing for an organization to be bound to; recording one anyway would leave the
        page claiming a list it does not have.
        """
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                handler = self.page[self.page.index(
                    f'if ({provider}OrgSelect && ({provider}OrgSelect.value || "") !== requestedOrg)'):]
                refused_at = handler.index("if (!result.ok || !result.data.ok) {")
                bound_at = handler.index(f"{provider}ListedOrg = requestedOrg;")
                self.assertGreater(
                    bound_at, refused_at,
                    "the organization is bound before the answer is known to carry a list")

    def test_the_scan_actually_reads_the_page(self):
        """A silent mismatch would make every check above pass for the wrong reason."""
        self.assertGreater(len(self.page), 10000)
        for provider in PROVIDERS:
            self.assertIn(f"{provider}ImportBtn", self.page)
