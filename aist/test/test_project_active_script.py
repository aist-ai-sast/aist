from __future__ import annotations

import html
import re

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from dojo.models import Product, Product_Type, SLA_Configuration
from rest_framework.test import APIClient

from aist.models import AISTProject, AISTProjectScript, AISTProjectVersion, VersionType

MAIN_SCRIPT = "#!/bin/bash\necho main"


class _ProjectsFixture(TestCase):
    def setUp(self):
        self.sla = SLA_Configuration.objects.create(name="SLA active")
        self.prod_type = Product_Type.objects.create(name="PT active")
        self.project = self._project("Active Product")

    def _project(self, name: str) -> AISTProject:
        product = Product.objects.create(
            name=name, description="d", prod_type=self.prod_type, sla_configuration_id=self.sla.id,
        )
        return AISTProject.objects.create(product=product, supported_languages=[], compilable=False, profile={})

    def _version(self, name, version_type=VersionType.GIT_BRANCH, script=None, project=None):
        return AISTProjectVersion.objects.create(
            project=project or self.project, version=name, version_type=version_type, script=script,
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

    def _active(self):
        resp = self.api.get(reverse("aist_api:project_active_script", kwargs={"project_id": self.project.id}))
        self.assertEqual(resp.status_code, 200)
        return resp.data

    def _row(self):
        content = self.client.get(reverse("aist:aist_project_list")).content.decode()
        row_start = content.index(f'<tr data-project-id="{self.project.id}"')
        row = content[row_start:content.index("</tr>", row_start)]
        attrs = {k: html.unescape(v) for k, v in re.findall(r'(data-project-active-[\w-]+)="([^"]*)"', row)}
        cell = re.search(r'<td class="script-cell">(.*?)</td>', row, re.DOTALL).group(1)
        return attrs, " ".join(re.sub(r"<[^>]+>", " ", html.unescape(cell)).split())

    def test_api_and_page_agree_on_branch_script(self):
        main = self._main_with_newer_bare_hash()

        data = self._active()
        attrs, cell = self._row()

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

        baseline = count_queries()
        for i in range(3):
            extra = self._project(f"Extra Product {i}")
            self._version("main", script=self._script(f"echo extra {i}", project=extra), project=extra)
            AISTProjectVersion.objects.create(
                project=extra, version=str(i) * 40, version_type=VersionType.GIT_HASH,
            )

        self.assertEqual(count_queries(), baseline)
