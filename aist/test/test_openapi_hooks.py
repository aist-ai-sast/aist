from __future__ import annotations

from django.test import SimpleTestCase

from aist_site.views import aist_only_preprocessing_hook, dojo_preprocessing_hook


class OpenApiHookTests(SimpleTestCase):
    def test_dojo_hook_keeps_dojo_paths_with_admin_prefix(self):
        endpoints = [
            ("/aist-admin/api/v2/findings/", None, "GET", object()),
            ("/aist-admin/api/v2/aist/findings/", None, "GET", object()),
            ("/aist-admin/api/v2/oa3/schema/", None, "GET", object()),
        ]

        filtered = dojo_preprocessing_hook(endpoints)
        paths = [path for path, _regex, _method, _callback in filtered]
        self.assertEqual(paths, ["/aist-admin/api/v2/findings/"])

    def test_aist_hook_keeps_aist_paths_with_admin_prefix(self):
        endpoints = [
            ("/aist-admin/api/v2/findings/", None, "GET", object()),
            ("/aist-admin/api/v2/aist/findings/", None, "GET", object()),
            ("/aist-admin/projects_version/1/files/blob/src.py", None, "GET", object()),
        ]

        filtered = aist_only_preprocessing_hook(endpoints)
        paths = [path for path, _regex, _method, _callback in filtered]
        self.assertEqual(
            paths,
            [
                "/aist-admin/api/v2/aist/findings/",
                "/aist-admin/projects_version/1/files/blob/src.py",
            ],
        )
