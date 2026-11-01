"""
The project a version is written to is the authorized one, never the submitted one.

``AISTProjectVersionForm`` pins the project by construction, so a submitted id cannot
redirect the write. The legacy view itself sits under the ``/aist-admin/`` prefix that
``AistAdminGuardMiddleware`` closes to every non-superuser.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product, Product_Type, Product_Type_Member, Role, SLA_Configuration

from aist.forms import AISTProjectVersionForm
from aist.models import AISTProject, AISTProjectVersion, Organization

User = get_user_model()


class ProjectVersionTenancyTests(TestCase):
    def setUp(self):
        self.sla = SLA_Configuration.objects.create(name="SLA")
        self.maintainer_role, _ = Role.objects.get_or_create(
            id=Roles.Maintainer, defaults={"name": "Maintainer"},
        )
        self.mine = self._project("Mine")
        self.theirs = self._project("Theirs")

        self.user = User.objects.create_user("attacker", "a@example.com", "pass")
        Product_Type_Member.objects.create(
            product_type=self.mine.product.prod_type, user=self.user, role=self.maintainer_role,
        )

    def _project(self, name):
        product_type = Product_Type.objects.create(name=name)
        Organization.objects.create(name=name, product_type=product_type)
        product = Product.objects.create(
            name=name, description="d", prod_type=product_type,
            sla_configuration_id=self.sla.id,
        )
        return AISTProject.objects.create(
            product=product, supported_languages=["python"], compilable=False, profile={},
        )

    def test_a_submitted_project_from_another_organization_is_ignored(self):
        """The write lands on the authorized project, or not at all."""
        form = AISTProjectVersionForm(
            {
                "project": str(self.theirs.pk),
                "version_type": "GIT_BRANCH",
                "version": "main",
                "description": "",
                "metadata": "{}",
            },
            project=self.mine,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["project"], self.mine)

        version = form.save()
        self.assertEqual(version.project, self.mine)
        self.assertFalse(AISTProjectVersion.objects.filter(project=self.theirs).exists())

    def test_a_form_built_without_a_project_can_not_create_anything(self):
        """A caller that forgets to pass the authorized project must fail closed."""
        form = AISTProjectVersionForm(
            {"project": str(self.mine.pk), "version_type": "GIT_BRANCH", "version": "main"},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("project", form.errors)

    def test_the_legacy_view_is_out_of_reach_for_a_non_superuser(self):
        """The page lives under the superuser-only prefix."""
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("aist:project_version_create", kwargs={"project_id": self.mine.pk}),
            data={"version_type": "GIT_BRANCH", "version": "main", "metadata": "{}"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AISTProjectVersion.objects.exists())

    def test_even_a_superuser_writes_the_url_project_not_the_payload_one(self):
        """Even the only caller that can reach the view cannot redirect the write."""
        root = User.objects.create_superuser("root", "root@example.com", "pass")
        self.client.force_login(root)
        response = self.client.post(
            reverse("aist:project_version_create", kwargs={"project_id": self.mine.pk}),
            data={
                "project": str(self.theirs.pk),
                "version_type": "GIT_BRANCH",
                "version": "main",
                "description": "",
                "metadata": "{}",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AISTProjectVersion.objects.filter(project=self.theirs).exists())
        self.assertTrue(AISTProjectVersion.objects.filter(project=self.mine).exists())
