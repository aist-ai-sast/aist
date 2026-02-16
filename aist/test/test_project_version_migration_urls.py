from __future__ import annotations

import importlib

from django.test import SimpleTestCase

migration_0012 = importlib.import_module("aist.migrations.0012_project_version_branch_hash_split")


class ProjectVersionMigrationUrlRewriteTests(SimpleTestCase):
    def test_replace_project_version_id_rewrites_blob_url(self):
        src = "https://example.local/api/v2/aist/projects_version/12/files/blob/src/main.py?x=1#L5"
        out = migration_0012._replace_project_version_id(src, 99)
        self.assertEqual(
            out,
            "https://example.local/api/v2/aist/projects_version/99/files/blob/src/main.py?x=1#L5",
        )

    def test_replace_project_version_id_ignores_non_blob_url(self):
        src = "https://example.local/api/v2/aist/projects_version/12/files/raw/src/main.py"
        out = migration_0012._replace_project_version_id(src, 99)
        self.assertIsNone(out)
