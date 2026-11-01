"""
Editing tags from the client UI.

Two surfaces write tags: the project card writes the tags of the project's
``Product`` (Maintainer and above), and a finding's Overview tab writes the
tags of that ``Finding`` (Writer and above). Both go through the same
normalisation, both refuse to touch the platform's own ``aist:`` tags, and
neither may reach an object outside the caller's organization.

Product tag inheritance is on platform-wide, so a project's tags are copied
onto its findings and cannot be edited there — that is what makes a project
tag usable as a finding filter. Every finding below therefore also carries its
project's ``backend``.
"""
from __future__ import annotations

from importlib import import_module
from unittest.mock import patch

from django.apps import apps as installed_apps
from django.contrib.auth import get_user_model
from django.core.cache import cache, caches
from django.db import migrations
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from dojo.authorization.roles_permissions import Roles
from dojo.models import (
    Engagement,
    Finding,
    Product,
    Product_Type,
    Product_Type_Member,
    Role,
    SLA_Configuration,
    System_Settings,
    Test,
    Test_Type,
    UserContactInfo,
)
from dojo.utils import get_system_setting
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from aist.api.tags import MAX_TAGS_PER_OBJECT
from aist.models import AISTProject, AISTProjectVersion, Organization, VersionType
from aist.utils.cache import QUEUE_CACHE_ALIAS

User = get_user_model()

# Imported by path because a migration module name starts with a digit.
TAG_INHERITANCE_MIGRATION = import_module("aist.migrations.0056_enable_product_tag_inheritance")


class TagTestCaseBase(TestCase):

    """
    Fixtures only, no tests of its own.

    Org A holds the project under test; Org B exists only to be unreachable.
    """

    def setUp(self):
        cache.clear()
        # A TransactionTestCase elsewhere in the suite flushes migration-seeded rows,
        # so the switch is asserted here rather than inherited from the database.
        # .first() bypasses the read-through cache; the manager's get() returns a
        # read-only snapshot that refuses to save.
        settings_row = System_Settings.objects.first() or System_Settings.objects.create()
        if not settings_row.enable_product_tag_inheritance:
            settings_row.enable_product_tag_inheritance = True
            settings_row.save(update_fields=["enable_product_tag_inheritance"])

        self.sla = SLA_Configuration.objects.create(name="SLA tag editing")
        self.test_type = Test_Type.objects.create(name="Tag editing test type")
        self.role_reader, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        self.role_writer, _ = Role.objects.get_or_create(id=Roles.Writer, defaults={"name": "Writer"})
        self.role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})

        self.pt_a = Product_Type.objects.create(name="Tag Org A")
        Organization.objects.create(name="Tag Org A", product_type=self.pt_a)
        self.pt_b = Product_Type.objects.create(name="Tag Org B")
        Organization.objects.create(name="Tag Org B", product_type=self.pt_b)

        self.maintainer = self._member("tag_maintainer", self.pt_a, self.role_maintainer)
        self.writer = self._member("tag_writer", self.pt_a, self.role_writer)
        self.reader = self._member("tag_reader", self.pt_a, self.role_reader)
        self.outsider = self._member("tag_outsider", self.pt_b, self.role_maintainer)

        self.project, self.finding = self._project_with_finding("Tag P1", self.pt_a)
        self.other_project, self.other_finding = self._project_with_finding("Tag B1", self.pt_b)

    # -- fixtures --------------------------------------------------------

    def _member(self, username, prod_type, role):
        user = User.objects.create_user(username, f"{username}@example.com", "pass")
        Product_Type_Member.objects.create(product_type=prod_type, user=user, role=role)
        return user

    def _project_with_finding(self, name, prod_type):
        product = Product.objects.create(
            name=name, description="d", prod_type=prod_type, sla_configuration_id=self.sla.id,
        )
        # Inherited by every finding below, and not removable there.
        product.tags = "backend"
        product.save()
        project = AISTProject.objects.create(
            product=product, supported_languages=["python"], compilable=False, profile={},
        )
        version = AISTProjectVersion.objects.create(
            project=project, version_type=VersionType.GIT_HASH, version=f"{name}-main",
        )
        now = timezone.now()
        engagement = Engagement.objects.create(
            name=f"{name} engagement", target_start=now, target_end=now, product=product,
        )
        test = Test.objects.create(
            engagement=engagement, target_start=now, target_end=now, test_type=self.test_type,
        )
        finding = Finding.objects.create(
            test=test,
            title=f"{name} finding",
            severity="High",
            active=True,
            file_path="src/app.py",
            reporter=self.maintainer,
        )
        finding.tags = "pci,aist:auto-triage"
        finding.save()
        version.findings.add(finding)
        return project, finding

    # -- helpers ---------------------------------------------------------

    def _client(self, user) -> APIClient:
        client = APIClient()
        client.force_login(user)
        return client

    def _put_project_tags(self, user, project, tags):
        return self._client(user).put(
            reverse("aist_api:project_tags_update", kwargs={"project_id": project.id}),
            data={"tags": tags},
            format="json",
        )

    def _put_finding_tags(self, user, finding, tags):
        return self._client(user).put(
            reverse("aist_api:finding_tags_update", kwargs={"finding_id": finding.id}),
            data={"tags": tags},
            format="json",
        )

    def _tags_of(self, obj) -> list[str]:
        obj.refresh_from_db()
        return sorted(tag.name for tag in obj.tags.all())


class TagEditingTestCase(TagTestCaseBase):

    """Who may write tags where, and what the API refuses to store."""

    # -- project tags ----------------------------------------------------

    def test_maintainer_replaces_project_tags_and_the_card_reads_them_back(self):
        response = self._put_project_tags(self.maintainer, self.project, ["PCI", " Team Payments "])
        self.assertEqual(response.status_code, 200, response.content)

        # force_lowercase on the model would do this anyway; the API does it up front.
        self.assertEqual(response.json()["tags"], ["pci", "team-payments"])
        self.assertEqual(self._tags_of(self.project.product), ["pci", "team-payments"])

        summary = self._client(self.maintainer).get(reverse("client_product_summary"))
        self.assertEqual(summary.status_code, 200, summary.content)
        rows = summary.json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        card = next(row for row in rows if row["project_id"] == self.project.id)
        self.assertEqual(sorted(card["tags"]), ["pci", "team-payments"])
        self.assertEqual(card["organization_id"], self.project.organization_id)

    def test_writer_may_not_edit_project_tags(self):
        response = self._put_project_tags(self.writer, self.project, ["pci"])
        self.assertIn(response.status_code, (403, 404), response.content)
        self.assertEqual(self._tags_of(self.project.product), ["backend"])

    def test_project_tags_of_another_organization_are_out_of_reach(self):
        response = self._put_project_tags(self.maintainer, self.other_project, ["leaked"])
        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(self._tags_of(self.other_project.product), ["backend"])

    # -- finding tags ----------------------------------------------------

    def test_writer_edits_finding_tags_without_touching_platform_tags(self):
        response = self._put_finding_tags(self.writer, self.finding, ["next-sprint"])
        self.assertEqual(response.status_code, 200, response.content)

        # "pci" was dropped by the client; "aist:auto-triage" is the platform's
        # and "backend" the project's, so both stay.
        self.assertEqual(response.json()["tags"], ["aist:auto-triage", "backend", "next-sprint"])
        self.assertEqual(self._tags_of(self.finding), ["aist:auto-triage", "backend", "next-sprint"])
        self.assertEqual(response.json()["project_tags"], ["backend"])

    def test_reader_may_not_edit_finding_tags(self):
        response = self._put_finding_tags(self.reader, self.finding, ["next-sprint"])
        self.assertIn(response.status_code, (403, 404), response.content)
        self.assertEqual(self._tags_of(self.finding), ["aist:auto-triage", "backend", "pci"])

    def test_finding_of_another_organization_is_out_of_reach(self):
        response = self._put_finding_tags(self.maintainer, self.other_finding, ["leaked"])
        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(self._tags_of(self.other_finding), ["aist:auto-triage", "backend", "pci"])

    # -- what the API refuses --------------------------------------------

    def test_a_comma_is_refused_instead_of_silently_splitting_a_tag(self):
        response = self._put_finding_tags(self.writer, self.finding, ["pci, backend"])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(self._tags_of(self.finding), ["aist:auto-triage", "backend", "pci"])

    def test_the_platform_prefix_is_refused(self):
        response = self._put_finding_tags(self.writer, self.finding, ["aist:mine"])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(self._tags_of(self.finding), ["aist:auto-triage", "backend", "pci"])

    def test_an_empty_tag_is_refused(self):
        response = self._put_finding_tags(self.writer, self.finding, ["   "])
        self.assertEqual(response.status_code, 400, response.content)

    def test_clearing_every_editable_tag_is_allowed(self):
        response = self._put_finding_tags(self.writer, self.finding, [])
        self.assertEqual(response.status_code, 200, response.content)
        # Only the finding's own tags go; the platform's and the project's stay.
        self.assertEqual(self._tags_of(self.finding), ["aist:auto-triage", "backend"])

    # -- the tag vocabulary ----------------------------------------------

    def test_a_new_tag_shows_up_in_the_picker_without_waiting_for_the_cache_to_expire(self):
        client = self._client(self.writer)
        vocabulary_url = reverse("aist_api:finding_tags")

        before = client.get(vocabulary_url)
        self.assertEqual(before.status_code, 200, before.content)
        self.assertNotIn("next-sprint", before.json()["tags"])

        with self.captureOnCommitCallbacks(execute=True):
            self._put_finding_tags(self.writer, self.finding, ["next-sprint"])

        after = client.get(vocabulary_url)
        self.assertEqual(after.status_code, 200, after.content)
        self.assertIn("next-sprint", after.json()["tags"])

    # -- one tag name, two organizations ---------------------------------

    def test_a_tag_name_shared_by_two_organizations_stays_separate_per_object(self):
        """Tag names are global; editing one organization's finding must not touch another's."""
        self.assertIn("pci", self._tags_of(self.finding))
        self.assertIn("pci", self._tags_of(self.other_finding))

        response = self._put_finding_tags(self.writer, self.finding, [])
        self.assertEqual(response.status_code, 200, response.content)

        self.assertNotIn("pci", self._tags_of(self.finding))
        self.assertEqual(self._tags_of(self.other_finding), ["aist:auto-triage", "backend", "pci"])

    def test_the_same_tag_name_does_not_make_another_organizations_object_writable(self):
        """A matching tag name is not a key: the object id still has to be in scope."""
        response = self._put_finding_tags(self.writer, self.other_finding, ["pci", "next-sprint"])
        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(self._tags_of(self.other_finding), ["aist:auto-triage", "backend", "pci"])

        project_response = self._put_project_tags(self.maintainer, self.other_project, ["backend"])
        self.assertEqual(project_response.status_code, 404, project_response.content)

    def test_the_tag_picker_only_offers_tags_from_the_callers_own_organization(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._put_finding_tags(self.outsider, self.other_finding, ["org-b-only"])

        vocabulary = self._client(self.writer).get(reverse("aist_api:finding_tags"))
        self.assertEqual(vocabulary.status_code, 200, vocabulary.content)
        payload = vocabulary.json()

        self.assertNotIn("org-b-only", payload["tags"])
        self.assertIn("pci", payload["tags"])
        # The count is the caller's own scope, not the global usage of the name.
        self.assertEqual(payload["counts"]["pci"], 1)

    def test_the_project_card_list_only_carries_tags_of_projects_in_scope(self):
        rows = self._client(self.writer).get(reverse("client_product_summary")).json()["results"]
        project_ids = {row["project_id"] for row in rows}

        self.assertIn(self.project.id, project_ids)
        self.assertNotIn(self.other_project.id, project_ids)


class TagInheritanceTestCase(TagTestCaseBase):

    """A tag put on a project reaches that project's findings."""

    def setUp(self):
        super().setUp()
        # block_execution runs the platform's propagation inside the request.
        UserContactInfo.objects.create(user=self.maintainer, block_execution=True)

    def _finding_payload(self, user, finding) -> dict:
        response = self._client(user).get(
            reverse("aist_api:finding_detail", kwargs={"finding_id": finding.id}),
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_the_migration_is_what_turns_inheritance_on(self):
        """The migration's own function is driven: the live setting only reports DB state."""
        settings_row = System_Settings.objects.first()
        settings_row.enable_product_tag_inheritance = False
        settings_row.save(update_fields=["enable_product_tag_inheritance"])

        TAG_INHERITANCE_MIGRATION.enable(installed_apps, None)
        self.assertTrue(get_system_setting("enable_product_tag_inheritance"))

    def test_reversing_the_migration_leaves_the_live_setting_alone(self):
        """Rolling back must not turn off a switch an operator may have set themselves."""
        operation = TAG_INHERITANCE_MIGRATION.Migration.operations[0]
        self.assertIs(operation.reverse_code, migrations.RunPython.noop)

    def test_a_project_tag_lands_on_the_findings_and_can_be_filtered_on(self):
        response = self._put_project_tags(self.maintainer, self.project, ["backend", "ai"])
        self.assertEqual(response.status_code, 200, response.content)

        self.assertIn("ai", self._tags_of(self.finding))

        listed = self._client(self.writer).get(
            reverse("aist_api:finding_list"), {"tags": "ai"},
        )
        self.assertEqual(listed.status_code, 200, listed.content)
        ids = [row["id"] for row in listed.json()["results"]]
        self.assertIn(self.finding.id, ids)
        self.assertNotIn(self.other_finding.id, ids)

    def test_the_finding_payload_says_which_tags_came_from_the_project(self):
        payload = self._finding_payload(self.writer, self.finding)
        self.assertEqual(payload["project_tags"], ["backend"])
        self.assertIn("backend", payload["tags"])
        # The finding's own tag is not reported as the project's.
        self.assertNotIn("pci", payload["project_tags"])

    def test_a_writer_cannot_drop_an_inherited_tag_from_a_finding(self):
        """An inherited tag is preserved, not removed and re-added by the platform."""
        response = self._put_finding_tags(self.writer, self.finding, ["pci"])
        self.assertEqual(response.status_code, 200, response.content)

        self.assertIn("backend", response.json()["tags"])
        self.assertEqual(response.json()["project_tags"], ["backend"])
        self.assertIn("backend", self._tags_of(self.finding))

    def test_an_inherited_tag_does_not_eat_into_the_per_object_tag_limit(self):
        own = [f"own-{index}" for index in range(MAX_TAGS_PER_OBJECT)]
        response = self._put_finding_tags(self.writer, self.finding, own)
        self.assertEqual(response.status_code, 200, response.content)

        stored = self._tags_of(self.finding)
        self.assertIn("backend", stored)
        self.assertEqual(len([tag for tag in stored if tag.startswith("own-")]), MAX_TAGS_PER_OBJECT)

    def test_taking_a_tag_off_the_project_takes_it_off_the_findings(self):
        self._put_project_tags(self.maintainer, self.project, ["backend", "ai"])
        self.assertIn("ai", self._tags_of(self.finding))

        response = self._put_project_tags(self.maintainer, self.project, ["backend"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn("ai", self._tags_of(self.finding))

    def test_a_project_tag_does_not_reach_another_organizations_findings(self):
        self._put_project_tags(self.maintainer, self.project, ["backend", "ai"])
        self.assertNotIn("ai", self._tags_of(self.other_finding))


class TagWriteRateLimitTestCase(TagTestCaseBase):

    """How often tags may be rewritten. Rates are lowered to two writes per scope."""

    def setUp(self):
        super().setUp()
        # Throttle counters live in the shared cache, which `cache.clear()` does not touch.
        caches[QUEUE_CACHE_ALIAS].clear()
        # `THROTTLE_RATES` is bound to the settings dict at import, so
        # `override_settings(REST_FRAMEWORK=...)` never reaches the throttle.
        rates = patch.dict(
            SimpleRateThrottle.THROTTLE_RATES,
            {"aist_project_tags": "2/hour", "aist_finding_tags": "2/hour"},
        )
        rates.start()
        self.addCleanup(rates.stop)

    def test_a_loop_of_project_tag_writes_is_cut_off(self):
        for attempt in range(2):
            response = self._put_project_tags(self.maintainer, self.project, [f"round-{attempt}"])
            self.assertEqual(response.status_code, 200, response.content)

        refused = self._put_project_tags(self.maintainer, self.project, ["round-3"])
        self.assertEqual(refused.status_code, 429, refused.content)
        # The write that was refused left nothing behind.
        self.assertEqual(self._tags_of(self.project.product), ["round-1"])

    def test_a_loop_of_finding_tag_writes_is_cut_off(self):
        for attempt in range(2):
            response = self._put_finding_tags(self.writer, self.finding, [f"round-{attempt}"])
            self.assertEqual(response.status_code, 200, response.content)

        refused = self._put_finding_tags(self.writer, self.finding, ["round-3"])
        self.assertEqual(refused.status_code, 429, refused.content)

    def test_the_two_budgets_are_separate(self):
        """Tagging findings must not spend the budget a project tag edit needs."""
        for attempt in range(2):
            self.assertEqual(
                self._put_finding_tags(self.writer, self.finding, [f"round-{attempt}"]).status_code,
                200,
            )
        self.assertEqual(
            self._put_finding_tags(self.writer, self.finding, ["blocked"]).status_code, 429,
        )

        allowed = self._put_project_tags(self.maintainer, self.project, ["still-allowed"])
        self.assertEqual(allowed.status_code, 200, allowed.content)

    def test_project_tag_writes_share_one_budget_per_project(self):
        """The cost falls on the project, so holding the role twice must not double the bound."""
        for attempt in range(2):
            self.assertEqual(
                self._put_project_tags(self.maintainer, self.project, [f"round-{attempt}"]).status_code,
                200,
            )

        second_maintainer = self._member("tag_maintainer_two", self.pt_a, self.role_maintainer)
        refused = self._put_project_tags(second_maintainer, self.project, ["from-someone-else"])
        self.assertEqual(refused.status_code, 429, refused.content)

        other_project, _finding = self._project_with_finding("Tag P2", self.pt_a)
        allowed = self._put_project_tags(second_maintainer, other_project, ["its-own-budget"])
        self.assertEqual(allowed.status_code, 200, allowed.content)

    def test_one_users_writes_do_not_spend_another_users_budget(self):
        for attempt in range(2):
            self.assertEqual(
                self._put_finding_tags(self.writer, self.finding, [f"round-{attempt}"]).status_code,
                200,
            )
        self.assertEqual(
            self._put_finding_tags(self.writer, self.finding, ["blocked"]).status_code, 429,
        )

        # A different Writer-or-above user still has their own allowance.
        other_writer = self._member("tag_writer_two", self.pt_a, self.role_writer)
        allowed = self._put_finding_tags(other_writer, self.finding, ["from-someone-else"])
        self.assertEqual(allowed.status_code, 200, allowed.content)
