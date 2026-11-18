"""Build-configuration edits preserve tenant and credential boundaries."""

from django.urls import reverse
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product

from aist.models import AISTProject, AISTProjectScript, AISTProjectVersion, ApiTokenScope
from aist.test.test_api_tokens import TokenTestBase

BASE_IMAGE = "registry.example/build/runtime@sha256:" + "a" * 64


class EnvironmentAuthorizationTests(TokenTestBase):
    def setUp(self):
        super().setUp()
        self.token_org = self._grant_write_role(self.user, role=Roles.Maintainer)
        self.other_org = self._grant_write_role(self.other, role=Roles.Maintainer)
        self.project = AISTProject.objects.create(product=Product.objects.get(prod_type=self.token_org.product_type))
        self.other_project = AISTProject.objects.create(product=Product.objects.get(prod_type=self.other_org.product_type))
        self.version = AISTProjectVersion.objects.create(project=self.project, version="main")
        self.other_version = AISTProjectVersion.objects.create(project=self.other_project, version="main")
        self.url = reverse("aist_api:project_script_list_create", kwargs={"project_id": self.project.pk})

    def test_organization_bound_superuser_token_cannot_modify_the_shared_default(self):
        shared = AISTProjectScript.get_shared_default()
        original = (shared.content, shared.base_image, shared.sha256)
        _, raw = self._issue(user=self.superuser, name="organization writer", scope=ApiTokenScope.READ_WRITE)

        response = self._bearer(raw).post(
            self.url, {"scope": "global", "content": "echo changed", "base_image": BASE_IMAGE}, format="json",
        )

        self.assertEqual(response.status_code, 403, response.data)
        shared.refresh_from_db()
        self.assertEqual((shared.content, shared.base_image, shared.sha256), original)
        self.assertEqual(self.other_version.effective_script.sha256, original[2])

    def test_organization_bound_superuser_token_can_still_edit_its_project(self):
        _, raw = self._issue(user=self.superuser, name="organization writer", scope=ApiTokenScope.READ_WRITE)

        response = self._bearer(raw).post(
            self.url, {"content": "echo local", "base_image": BASE_IMAGE}, format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.version.refresh_from_db()
        self.assertEqual(self.version.script_id, response.data["id"])
        self.assertEqual(self.version.script.base_image, BASE_IMAGE)
        self.other_version.refresh_from_db()
        self.assertIsNone(self.other_version.script_id)

    def test_maintainer_cannot_copy_or_edit_another_organizations_revision(self):
        foreign = AISTProjectScript.objects.create(project=self.other_project, content="echo other configuration")
        self.other_version.script = foreign
        self.other_version.save(update_fields=["script", "updated"])
        _, raw = self._issue(user=self.user, name="project writer", scope=ApiTokenScope.READ_WRITE)
        foreign_url = reverse("aist_api:project_script_list_create", kwargs={"project_id": self.other_project.pk})

        for authentication, client in (("session", self._session(self.user)), ("token", self._bearer(raw))):
            with self.subTest(authentication=authentication):
                response = client.post(
                    self.url, {"source_script_id": foreign.pk, "base_image": BASE_IMAGE}, format="json",
                )
                self.assertEqual(response.status_code, 400, response.data)
                response = client.post(
                    foreign_url, {"content": "echo denied", "base_image": BASE_IMAGE}, format="json",
                )
                self.assertEqual(response.status_code, 404, response.data)

        self.version.refresh_from_db()
        self.other_version.refresh_from_db()
        foreign.refresh_from_db()
        self.assertIsNone(self.version.script_id)
        self.assertEqual(self.other_version.script_id, foreign.pk)
        self.assertEqual(foreign.content, "echo other configuration")
        self.assertEqual(foreign.base_image, "")
        self.assertFalse(self.project.script_revisions.exists())
        self.assertEqual(self.other_project.script_revisions.count(), 1)
