from __future__ import annotations

from django.urls import reverse
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product, Product_Type, Product_Type_Member, Role
from rest_framework.test import APIClient

from aist.models import AISTProject, AISTProjectScript, AISTProjectVersion, Organization, VersionType
from aist.test.test_api import AISTApiBase


class AISTProjectCreateAPITests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.url = reverse("aist_api:project_list")

    def _make_maintainer_org(self, suffix: str) -> Organization:
        pt = Product_Type.objects.create(name=f"PT {suffix}")
        role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=pt, user=self.user, role=role_maintainer)
        return Organization.objects.create(name=f"Org {suffix}", product_type=pt)

    def test_create_empty_project_success(self):
        org = self._make_maintainer_org("PT")

        resp = self.client.post(
            self.url,
            data={
                "organization_id": org.id,
                "product_name": "New Empty Product",
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data["ok"])
        project_id = resp.data["project"]["id"]
        project = AISTProject.objects.get(id=project_id)
        self.assertEqual(project.organization_id, org.id)
        self.assertEqual(project.product.name, "New Empty Product")
        # Default script is always created as a project-scoped revision on project creation.
        active = project.active_script
        self.assertIsNotNone(active)
        self.assertGreater(len(active.content), 0)

    def test_create_project_without_script_uses_shared_default_content(self):
        """Projects created without custom script content each get a project-scoped copy of the shared default."""
        org_a = self._make_maintainer_org("SharedA")
        org_b = self._make_maintainer_org("SharedB")

        resp_a = self.client.post(self.url, data={"organization_id": org_a.id, "product_name": "Proj A"}, format="json")
        resp_b = self.client.post(self.url, data={"organization_id": org_b.id, "product_name": "Proj B"}, format="json")

        self.assertEqual(resp_a.status_code, 201)
        self.assertEqual(resp_b.status_code, 201)

        proj_a = AISTProject.objects.get(id=resp_a.data["project"]["id"])
        proj_b = AISTProject.objects.get(id=resp_b.data["project"]["id"])

        # Each project gets its own project-scoped copy; they have the same content
        # as the shared default but are distinct records (org-isolated).
        shared = AISTProjectScript.get_shared_default()
        self.assertEqual(proj_a.active_script.content, shared.content)
        self.assertEqual(proj_b.active_script.content, shared.content)
        self.assertFalse(proj_a.active_script.is_shared)
        self.assertFalse(proj_b.active_script.is_shared)

    def test_shared_default_singleton_exists_after_creation(self):
        """get_shared_default() returns the same singleton on repeated calls."""
        org = self._make_maintainer_org("Singleton")
        self.client.post(self.url, data={"organization_id": org.id, "product_name": "Singleton Proj"}, format="json")

        s1 = AISTProjectScript.get_shared_default()
        s2 = AISTProjectScript.get_shared_default()
        self.assertEqual(s1.id, s2.id)
        self.assertTrue(s1.is_shared)
        self.assertIsNone(s1.project_id)

    def test_create_project_with_custom_script_content(self):
        target_pt = Product_Type.objects.create(name="Org SC")
        role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=target_pt, user=self.user, role=role_maintainer)
        org = Organization.objects.create(name="Org SC", product_type=target_pt)
        custom_content = "#!/bin/bash\necho custom"

        resp = self.client.post(
            self.url,
            data={
                "organization_id": org.id,
                "product_name": "Custom Script Product",
                "script_content": custom_content,
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201)
        project = AISTProject.objects.get(id=resp.data["project"]["id"])
        self.assertIsNotNone(project.active_script)
        self.assertEqual(project.active_script.content, custom_content)

    def test_create_empty_project_forbidden_without_add_permission(self):
        target_pt = Product_Type.objects.create(name="Reader PT")
        role_reader, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        Product_Type_Member.objects.create(product_type=target_pt, user=self.user, role=role_reader)
        org = Organization.objects.create(name="Org Reader", product_type=target_pt)

        resp = self.client.post(
            self.url,
            data={
                "organization_id": org.id,
                "product_name": "Should Not Create",
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Product.objects.filter(name="Should Not Create").exists())

    def test_create_empty_project_conflict_when_product_in_other_product_type(self):
        pt_a = Product_Type.objects.create(name="PT A")
        pt_b = Product_Type.objects.create(name="PT B")
        role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=pt_a, user=self.user, role=role_maintainer)
        Product_Type_Member.objects.create(product_type=pt_b, user=self.user, role=role_maintainer)
        Organization.objects.create(name="Org A", product_type=pt_a)
        org_b = Organization.objects.create(name="Org B", product_type=pt_b)

        Product.objects.create(
            name="Shared Product",
            prod_type=pt_a,
            description="existing",
            sla_configuration_id=self.sla.id,
        )

        resp = self.client.post(
            self.url,
            data={
                "organization_id": org_b.id,
                "product_name": "Shared Product",
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 409)

    def test_create_empty_project_conflict_when_aist_project_exists(self):
        org = Organization.objects.create(name="Org Existing", product_type=self.prod_type)

        resp = self.client.post(
            self.url,
            data={
                "organization_id": org.id,
                "product_name": self.product.name,
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 409)


class AISTProjectActiveScriptAPITests(AISTApiBase):

    """Tests for GET /projects/<id>/active_script/ endpoint."""

    def _url(self, project_id: int) -> str:
        return reverse("aist_api:project_active_script", kwargs={"project_id": project_id})

    def test_returns_shared_default_content(self):
        """The fixture project (no version script, no project revision) falls back to shared default."""
        # self.pv has script=None and no project-scoped revisions exist → property returns shared default.
        resp = self.client.get(self._url(self.project.id))

        self.assertEqual(resp.status_code, 200)
        self.assertIn("content", resp.data)
        self.assertGreater(len(resp.data["content"]), 0)
        self.assertTrue(resp.data["is_shared"])

    def test_returns_project_specific_script(self):
        """When the latest version has a project-scoped script, is_shared must be False."""
        script = AISTProjectScript.objects.create(
            project=self.project,
            content="#!/bin/bash\necho custom",
            is_shared=False,
        )
        self.pv.script = script
        self.pv.save(update_fields=["script", "updated"])

        resp = self.client.get(self._url(self.project.id))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["content"], "#!/bin/bash\necho custom")
        self.assertFalse(resp.data["is_shared"])

    def test_always_returns_200_active_script(self):
        """active_script always resolves (shared default fallback) — endpoint always returns 200."""
        # No version script, no project revision → shared default
        resp = self.client.get(self._url(self.project.id))
        self.assertEqual(resp.status_code, 200)

    def test_unauthenticated_returns_403(self):
        anon = APIClient()
        resp = anon.get(self._url(self.project.id))
        self.assertIn(resp.status_code, [401, 403])

    def test_inherited_flag_when_shared_default(self):
        """Shared-default fallback must be flagged as inherited with source=shared_default."""
        resp = self.client.get(self._url(self.project.id))

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["inherited"])
        self.assertEqual(resp.data["source"], "shared_default")

    def test_inherited_flag_when_project_revision(self):
        """When project has a revision but no version-attached script, source=project_revision."""
        AISTProjectScript.objects.create(
            project=self.project,
            is_shared=False,
            content="#!/bin/bash\necho proj-rev",
        )

        resp = self.client.get(self._url(self.project.id))

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["inherited"])
        self.assertEqual(resp.data["source"], "project_revision")

    def test_inherited_flag_false_when_version_has_script(self):
        """When the latest version has its own script, inherited=False and source=version."""
        script = AISTProjectScript.objects.create(
            project=self.project,
            is_shared=False,
            content="#!/bin/bash\necho version-own",
        )
        self.pv.script = script
        self.pv.save(update_fields=["script", "updated"])

        resp = self.client.get(self._url(self.project.id))

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["inherited"])
        self.assertEqual(resp.data["source"], "version")


class AISTProjectScriptScopeAPITests(AISTApiBase):

    """Tests for scope=local / scope=global on POST /projects/<id>/scripts/."""

    def _scripts_url(self, project_id: int) -> str:
        return reverse("aist_api:project_script_list_create", kwargs={"project_id": project_id})

    def setUp(self):
        super().setUp()
        self.shared = AISTProjectScript.get_shared_default()
        # Make user a superuser so global-scope tests can update the shared singleton.
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])

    # --- scope=local ---

    def test_local_scope_creates_project_specific_script(self):
        resp = self.client.post(
            self._scripts_url(self.project.id),
            data={"content": "#!/bin/bash\necho local", "scope": "local"},
            format="json",
        )

        self.assertEqual(resp.status_code, 201)
        self.assertFalse(resp.data["is_shared"])
        # active_script property reads fresh from DB each call; latest version script is updated.
        self.assertEqual(self.project.active_script.content, "#!/bin/bash\necho local")
        # Shared default unchanged.
        self.shared.refresh_from_db()
        self.assertNotEqual(self.shared.content, "#!/bin/bash\necho local")

    def test_local_scope_detaches_project_from_shared_default(self):
        resp = self.client.post(
            self._scripts_url(self.project.id),
            data={"content": "#!/bin/bash\necho mine", "scope": "local"},
            format="json",
        )

        self.assertEqual(resp.status_code, 201)
        # active_script is now a project-scoped record (different from the shared singleton).
        self.assertNotEqual(self.project.active_script.id, self.shared.id)

    def test_local_scope_is_default(self):
        """Omitting scope behaves the same as scope=local."""
        resp = self.client.post(
            self._scripts_url(self.project.id),
            data={"content": "#!/bin/bash\necho default-scope"},
            format="json",
        )

        self.assertEqual(resp.status_code, 201)
        self.assertFalse(resp.data["is_shared"])

    # --- scope=global ---

    def test_global_scope_updates_shared_singleton_in_place(self):
        new_content = "#!/bin/bash\necho updated-global"
        resp = self.client.post(
            self._scripts_url(self.project.id),
            data={"content": new_content, "scope": "global"},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["is_shared"])
        # The singleton record itself was updated.
        self.shared.refresh_from_db()
        self.assertEqual(self.shared.content, new_content)

    def test_global_scope_same_script_id(self):
        """Global update must not create a new record — it reuses the singleton's PK."""
        original_id = self.shared.id
        resp = self.client.post(
            self._scripts_url(self.project.id),
            data={"content": "#!/bin/bash\necho still-global", "scope": "global"},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["id"], original_id)

    def test_global_scope_updates_shared_singleton_content(self):
        """Global update modifies the shared singleton in-place; get_shared_default() reflects new content."""
        new_content = "#!/bin/bash\necho propagated"

        resp = self.client.post(
            self._scripts_url(self.project.id),
            data={"content": new_content, "scope": "global"},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.shared.refresh_from_db()
        self.assertEqual(self.shared.content, new_content)
        # Singleton identity preserved.
        self.assertEqual(AISTProjectScript.get_shared_default().id, self.shared.id)

    def test_global_scope_returns_404_when_singleton_missing(self):
        """scope=global must return 404 (not 500) when no shared default exists."""
        AISTProjectScript.objects.filter(is_shared=True).delete()

        resp = self.client.post(
            self._scripts_url(self.project.id),
            data={"content": "#!/bin/bash\necho x", "scope": "global"},
            format="json",
        )

        self.assertEqual(resp.status_code, 404)

    def test_script_detail_returns_shared_script(self):
        """GET /projects/<id>/scripts/<shared_id>/ must work for the shared singleton."""
        script_url = reverse(
            "aist_api:project_script_detail",
            kwargs={"project_id": self.project.id, "script_id": self.shared.id},
        )

        resp = self.client.get(script_url)

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["is_shared"])
        self.assertIn("content", resp.data)

    def test_script_detail_returns_404_for_other_project_script(self):
        """A script belonging to a different project must not be visible."""
        other_product = Product.objects.create(
            name="Other Product 2",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )
        other_project = AISTProject.objects.create(
            product=other_product,
            supported_languages=[],
            compilable=False,
            profile={},
        )
        other_script = AISTProjectScript.objects.create(
            project=other_project,
            content="#!/bin/bash\necho other",
            is_shared=False,
        )

        script_url = reverse(
            "aist_api:project_script_detail",
            kwargs={"project_id": self.project.id, "script_id": other_script.id},
        )
        resp = self.client.get(script_url)

        self.assertEqual(resp.status_code, 404)


class AISTProjectCreateScriptValidationTests(AISTApiBase):

    """Script content submitted at project creation must be validated."""

    def setUp(self):
        super().setUp()
        self.url = reverse("aist_api:project_list")
        pt = Product_Type.objects.create(name="PT ValScript")
        role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=pt, user=self.user, role=role_maintainer)
        self.org = Organization.objects.create(name="Org ValScript", product_type=pt)

    def test_oversized_script_content_returns_400(self):
        """A script exceeding the 256 KB limit must be rejected at project creation."""
        big_content = "x" * (256 * 1024 + 1)

        resp = self.client.post(
            self.url,
            data={
                "organization_id": self.org.id,
                "product_name": "Too Big Script Project",
                "script_content": big_content,
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 400)


class ProjectVersionScriptUpdateAPITests(AISTApiBase):

    """Tests for PATCH /projects/<id>/versions/<vid>/script endpoint."""

    def setUp(self):
        super().setUp()
        pt = Product_Type.objects.create(name="PT VersionScript")
        role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=pt, user=self.user, role=role_maintainer)
        self.org = Organization.objects.create(name="Org VersionScript", product_type=pt)

        resp = self.client.post(
            reverse("aist_api:project_list"),
            data={"organization_id": self.org.id, "product_name": "VS Product"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.project = AISTProject.objects.get(id=resp.data["project"]["id"])

        self.version = AISTProjectVersion.objects.create(
            project=self.project,
            version="v1.0",
            version_type=VersionType.GIT_BRANCH,
        )

    def _patch_url(self):
        return reverse(
            "aist_api:project_version_script_update",
            kwargs={"project_id": self.project.id, "version_id": self.version.id},
        )

    def test_set_version_script_to_project_script(self):
        """PATCH with a valid script_id sets version.script_id."""
        script = AISTProjectScript.objects.create(
            project=self.project,
            content="#!/bin/bash\necho v1",
        )

        resp = self.client.patch(
            self._patch_url(),
            data={"script_id": script.id},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["script_id"], script.id)
        self.version.refresh_from_db()
        self.assertEqual(self.version.script_id, script.id)

    def test_clear_version_script_with_null(self):
        """PATCH with script_id=null clears the override."""
        script = AISTProjectScript.objects.create(
            project=self.project,
            content="#!/bin/bash\necho v1",
        )
        self.version.script = script
        self.version.save(update_fields=["script", "updated"])

        resp = self.client.patch(
            self._patch_url(),
            data={"script_id": None},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data["script_id"])
        self.version.refresh_from_db()
        self.assertIsNone(self.version.script_id)

    def test_set_shared_default_script_on_version_rejected(self):
        """PATCH with the shared default script_id must be rejected — versions require project-scoped scripts."""
        shared = AISTProjectScript.get_shared_default()

        resp = self.client.patch(
            self._patch_url(),
            data={"script_id": shared.id},
            format="json",
        )

        self.assertEqual(resp.status_code, 400)

    def test_cross_project_script_rejected(self):
        """PATCH with a script from another project returns 400."""
        other_pt = Product_Type.objects.create(name="PT Other VS")
        Product_Type_Member.objects.create(
            product_type=other_pt,
            user=self.user,
            role=Role.objects.get(id=Roles.Maintainer),
        )
        other_org = Organization.objects.create(name="Other Org VS", product_type=other_pt)
        other_resp = self.client.post(
            reverse("aist_api:project_list"),
            data={"organization_id": other_org.id, "product_name": "Other VS Product"},
            format="json",
        )
        other_project = AISTProject.objects.get(id=other_resp.data["project"]["id"])
        foreign_script = AISTProjectScript.objects.create(
            project=other_project,
            content="#!/bin/bash\necho foreign",
        )

        resp = self.client.patch(
            self._patch_url(),
            data={"script_id": foreign_script.id},
            format="json",
        )

        self.assertEqual(resp.status_code, 400)
