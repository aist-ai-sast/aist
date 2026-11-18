from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from dojo.models import Product_Type, SLA_Configuration
from lxml import html
from rest_framework.test import APIClient

from aist.models import AISTProject, AISTProjectScript, AISTProjectVersion, VersionType
from aist.test.project_fixtures import create_project, create_project_version

MAIN_SCRIPT = "#!/bin/bash\necho main"
BASE_IMAGE = "ubuntu:24.04@sha256:" + "a" * 64


class _ProjectsFixture(TestCase):
    def setUp(self):
        self.sla = SLA_Configuration.objects.create(name="SLA active")
        self.prod_type = Product_Type.objects.create(name="PT active")
        self.project = self._project("Active Product")

    def _project(self, name: str) -> AISTProject:
        return create_project(name=name, product_type=self.prod_type, sla=self.sla)

    def _version(self, name, version_type=VersionType.GIT_BRANCH, script=None, project=None):
        return create_project_version(
            project=project or self.project, name=name, version_type=version_type, script=script,
        )

    def _script(self, content, project=None):
        return AISTProjectScript.objects.create(project=project or self.project, content=content)

    def _main_with_newer_bare_hash(self):
        main = self._version("main", script=self._script(MAIN_SCRIPT))
        AISTProjectVersion.objects.create(
            project=self.project, version="c" * 40, version_type=VersionType.GIT_HASH,
            resolved_from_branch=main, script=None,
        )
        return main


class ActiveScriptRuleTests(_ProjectsFixture):

    """active_script is what the next branch run uses — checked on the query path and the prefetch path."""

    def _both_paths(self):
        fresh = AISTProject.objects.get(pk=self.project.pk)
        prefetched = AISTProject.objects.get(pk=self.project.pk)
        AISTProject.prefetch_active_scripts([prefetched])
        return {"query": fresh, "prefetch": prefetched}

    def assertActive(self, *, version, script):
        for path, project in self._both_paths().items():
            with self.subTest(path=path):
                self.assertEqual(project.active_version, version)
                self.assertEqual(project.active_script, script)

    def test_branch_script_wins_over_newer_bare_commit_version(self):
        main = self._main_with_newer_bare_hash()

        self.assertActive(version=main, script=main.script)

    def test_newest_branch_wins(self):
        self._version("main", script=self._script("echo old-branch"))
        release = self._version("release", script=self._script("echo new-branch"))

        self.assertActive(version=release, script=release.script)

    def test_without_branches_newest_sourced_version_is_used_and_dast_ignored(self):
        self._version("a" * 64, VersionType.FILE_HASH, script=self._script("echo old-archive"))
        newest_archive = self._version("b" * 64, VersionType.FILE_HASH, script=self._script("echo new-archive"))
        self._version("https://app.example", VersionType.DAST_TARGET)

        self.assertActive(version=newest_archive, script=newest_archive.script)

    def test_branch_without_script_runs_shared_default(self):
        main = self._version("main")

        self.assertActive(version=main, script=AISTProjectScript.get_shared_default())

    def test_no_versions_and_unbound_revision_show_shared_default(self):
        self._script("echo unbound revision")

        self.assertActive(version=None, script=AISTProjectScript.get_shared_default())


class ActiveScriptApiTests(_ProjectsFixture):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_superuser(
            username="active_admin", password="pass", email="active_admin@example.com",  # noqa: S106
        )
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.client.force_login(self.user)

    def test_environment_only_edit_creates_revision_and_version_can_restore_previous_pair(self):
        content = MAIN_SCRIPT + "\n"
        original = self._script(content)
        main = self._version("main", script=original)
        url = reverse("aist_api:project_script_list_create", kwargs={"project_id": self.project.id})
        response = self.api.post(url, {"content": content, "base_image": BASE_IMAGE}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self._active()["base_image"], BASE_IMAGE)
        self.assertEqual(self._active()["content"], content)
        self.assertNotEqual(response.data["sha256"], original.sha256)
        version_url = reverse("aist_api:project_version_script_update", kwargs={
            "project_id": self.project.id, "version_id": main.id,
        })
        self.assertEqual(self.api.get(version_url).data["base_image"], BASE_IMAGE)
        self.assertEqual(self.api.patch(version_url, {"script_id": original.id}, format="json").status_code, 200)
        self.assertEqual(self._active()["base_image"], "")
        self.assertEqual(self._active()["content"], content)

    def test_older_client_edit_keeps_active_base_image(self):
        script = AISTProjectScript.objects.create(project=self.project, content=MAIN_SCRIPT, base_image=BASE_IMAGE)
        self._version("main", script=script)
        url = reverse("aist_api:project_script_list_create", kwargs={"project_id": self.project.id})
        response = self.api.post(url, {"content": "echo changed"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["base_image"], BASE_IMAGE)

    def test_environment_only_edit_preserves_masked_crlf_revision_when_branch_changes(self):
        content = "#!/bin/bash\r\nexport API_TOKEN=fixture-secret\r\necho ready\r\n"
        original = self._script(content)
        main = self._version("main", script=original)
        visible = self._active()
        self.assertNotIn("fixture-secret", visible["content"])
        main.script = self._script("echo newer revision")
        main.save(update_fields=["script"])
        url = reverse("aist_api:project_script_list_create", kwargs={"project_id": self.project.id})
        response = self.api.post(url, {
            "source_script_id": visible["id"], "base_image": BASE_IMAGE,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        revision = self.project.script_revisions.get(pk=response.data["id"])
        self.assertEqual(revision.content, content)
        self.assertEqual(revision.base_image, BASE_IMAGE)
        self.assertNotIn("fixture-secret", response.data["content"])

    def test_global_environment_only_edit_preserves_shared_script_content(self):
        shared = AISTProjectScript.get_shared_default()
        shared.content = "#!/bin/bash\nexport API_TOKEN=fixture-secret\n"
        shared.save(update_fields=["content"])
        url = reverse("aist_api:project_script_list_create", kwargs={"project_id": self.project.id})
        response = self.api.post(url, {
            "scope": "global", "source_script_id": shared.id, "base_image": BASE_IMAGE,
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        shared.refresh_from_db()
        self.assertEqual(shared.content, "#!/bin/bash\nexport API_TOKEN=fixture-secret\n")
        self.assertEqual(shared.base_image, BASE_IMAGE)
        self.assertNotIn("fixture-secret", response.data["content"])

    def test_environment_revision_cannot_copy_another_projects_script(self):
        foreign = self._script("echo foreign", project=self._project("Other Product"))
        url = reverse("aist_api:project_script_list_create", kwargs={"project_id": self.project.id})
        response = self.api.post(url, {
            "source_script_id": foreign.id, "base_image": BASE_IMAGE,
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("source_script_id", response.data)
        self.assertFalse(self.project.script_revisions.exists())

    def test_unpinned_or_malformed_base_image_is_rejected_without_creating_revision(self):
        url = reverse("aist_api:project_script_list_create", kwargs={"project_id": self.project.id})
        for image in (
            "ubuntu:24.04", "ubuntu@sha256:abc", "--flag@sha256:" + "a" * 64,
            "/@sha256:" + "a" * 64, "namespace/UPPER@sha256:" + "a" * 64,
            "registry.example/ns//runtime@sha256:" + "a" * 64,
        ):
            with self.subTest(image=image):
                response = self.api.post(url, {"content": MAIN_SCRIPT, "base_image": image}, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertIn("base_image", response.data)
        self.assertFalse(self.project.script_revisions.exists())

    def test_registry_port_tag_and_ipv6_images_are_saved_without_rewriting(self):
        url = reverse("aist_api:project_script_list_create", kwargs={"project_id": self.project.id})
        for name in ("registry.example:5000/team/runtime:stable", "[::1]:5000/team/runtime", "REGISTRY/repo"):
            image = name + "@sha256:" + "a" * 64
            with self.subTest(image=image):
                response = self.api.post(url, {"content": MAIN_SCRIPT, "base_image": image}, format="json")
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data["base_image"], image)
                self.assertEqual(self.project.script_revisions.get(pk=response.data["id"]).base_image, image)

    def _active(self):
        resp = self.api.get(reverse("aist_api:project_active_script", kwargs={"project_id": self.project.id}))
        self.assertEqual(resp.status_code, 200)
        return resp.data

    def _row(self):
        content = self.client.get(reverse("aist:aist_project_list")).content
        document = html.fromstring(content)
        row = document.xpath("//tr[@data-project-id=$project_id]", project_id=str(self.project.id))[0]
        cell = row.xpath(".//td[@class='script-cell']")[0]
        return dict(row.attrib), " ".join(" ".join(cell.itertext()).split())

    def test_api_and_page_agree_on_branch_script(self):
        self.project.profile = {"file_exclusions": ["owner's/<generated>"], "nested": {"enabled": False}}
        self.project.save(update_fields=["profile"])
        main = self._main_with_newer_bare_hash()

        data = self._active()
        attrs, cell = self._row()

        self.assertEqual(json.loads(attrs["data-project-profile"]), self.project.profile)
        self.assertEqual(data["id"], main.script_id)
        self.assertEqual(data["source"], "version")
        self.assertFalse(data["inherited"])
        self.assertEqual(data["version"], {"id": main.id, "version": "main", "type": "GIT_BRANCH"})
        self.assertEqual(attrs["data-project-active-script-id"], str(main.script_id))
        self.assertEqual(attrs["data-project-active-version-id"], str(main.id))
        self.assertEqual(cell, f"{main.script.sha256[:8]}… main")

    def test_branch_without_script_is_reported_as_shared_default(self):
        main = self._version("main")
        shared = AISTProjectScript.get_shared_default()

        data = self._active()
        attrs, cell = self._row()

        self.assertEqual(data["id"], shared.id)
        self.assertEqual(data["source"], "shared_default")
        self.assertTrue(data["inherited"])
        self.assertEqual(data["version"]["id"], main.id)
        self.assertEqual(attrs["data-project-active-version-id"], str(main.id))
        self.assertEqual(cell, f"{shared.sha256[:8]}… default")

    def test_project_without_versions(self):
        data = self._active()
        attrs, cell = self._row()

        self.assertIsNone(data["version"])
        self.assertEqual(data["source"], "shared_default")
        self.assertEqual(attrs["data-project-active-version-id"], "")
        self.assertTrue(cell.endswith("default"))

    def test_saving_with_set_active_binds_the_branch_not_the_newest_commit(self):
        main = self._main_with_newer_bare_hash()
        bare_hash = self.project.versions.get(version_type=VersionType.GIT_HASH)

        resp = self.api.post(
            reverse("aist_api:project_script_list_create", kwargs={"project_id": self.project.id}),
            data={"content": "#!/bin/bash\necho edited", "scope": "local", "set_active": True},
            format="json",
        )

        self.assertEqual(resp.status_code, 201, resp.data)
        main.refresh_from_db()
        bare_hash.refresh_from_db()
        self.assertEqual(main.script_id, resp.data["id"])
        self.assertIsNone(bare_hash.script_id)
        self.assertEqual(self._active()["id"], resp.data["id"])

    def test_project_list_query_count_does_not_grow_with_projects(self):
        self._main_with_newer_bare_hash()

        def count_queries():
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(reverse("aist:aist_project_list")).status_code, 200)
            return len(ctx.captured_queries)

        # Discarded: the first call also warms per-process caches (system settings,
        # content types), which are not part of the per-project cost under test.
        count_queries()
        baseline = count_queries()
        for i in range(3):
            extra = self._project(f"Extra Product {i}")
            self._version("main", script=self._script(f"echo extra {i}", project=extra), project=extra)
            AISTProjectVersion.objects.create(
                project=extra, version=str(i) * 40, version_type=VersionType.GIT_HASH,
            )

        self.assertEqual(count_queries(), baseline)
