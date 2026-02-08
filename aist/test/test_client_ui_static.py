from __future__ import annotations

from pathlib import Path

from django.test import SimpleTestCase


class ClientUIStaticTests(SimpleTestCase):
    def test_client_ui_placeholder_exists(self):
        base_dir = Path(__file__).resolve().parents[2]
        index = base_dir / "client-ui" / "index.html"
        self.assertTrue(index.exists())
        self.assertIn("AIST Client UI", index.read_text(encoding="utf-8"))

    def test_client_ui_detail_page_exists(self):
        base_dir = Path(__file__).resolve().parents[2]
        detail = base_dir / "client-ui" / "src" / "pages" / "FindingDetailPage.tsx"
        self.assertTrue(detail.exists())
        self.assertIn("Finding Detail", detail.read_text(encoding="utf-8"))

    def test_client_ui_api_queries_present(self):
        base_dir = Path(__file__).resolve().parents[2]
        routes = base_dir / "client-ui" / "src" / "lib" / "routes.ts"
        self.assertTrue(routes.exists())
        content = routes.read_text(encoding="utf-8")
        self.assertIn("__AIST_ROUTES__", content)

    def test_client_ui_monaco_present(self):
        base_dir = Path(__file__).resolve().parents[2]
        code_snippet = base_dir / "client-ui" / "src" / "components" / "CodeSnippet.tsx"
        self.assertTrue(code_snippet.exists())
        content = code_snippet.read_text(encoding="utf-8")
        self.assertIn("@monaco-editor/react", content)
        self.assertIn("Expand", content)

    def test_client_ui_select_field_present(self):
        base_dir = Path(__file__).resolve().parents[2]
        select_field = base_dir / "client-ui" / "src" / "components" / "SelectField.tsx"
        self.assertTrue(select_field.exists())
        self.assertIn("@radix-ui/react-select", select_field.read_text(encoding="utf-8"))

    def test_client_ui_multi_select_present(self):
        base_dir = Path(__file__).resolve().parents[2]
        multi_select = base_dir / "client-ui" / "src" / "components" / "MultiSelectChips.tsx"
        self.assertTrue(multi_select.exists())

    def test_client_ui_description_block_present(self):
        base_dir = Path(__file__).resolve().parents[2]
        block = base_dir / "client-ui" / "src" / "components" / "DescriptionBlock.tsx"
        self.assertTrue(block.exists())

    def test_client_ui_permissions_present(self):
        base_dir = Path(__file__).resolve().parents[2]
        permissions = base_dir / "client-ui" / "src" / "lib" / "permissions.ts"
        self.assertTrue(permissions.exists())
        content = permissions.read_text(encoding="utf-8")
        self.assertIn("useWritePermissions", content)
        gate = base_dir / "client-ui" / "src" / "components" / "PermissionGate.tsx"
        self.assertTrue(gate.exists())
        self.assertIn("PermissionGate", gate.read_text(encoding="utf-8"))

    def test_client_ui_auth_hook_present(self):
        base_dir = Path(__file__).resolve().parents[2]
        app_file = base_dir / "client-ui" / "src" / "App.tsx"
        self.assertTrue(app_file.exists())
        content = app_file.read_text(encoding="utf-8")
        self.assertIn("RequireAuth", content)
        self.assertIn("LoginPage", content)

    def test_client_ui_auth_proxy_paths(self):
        base_dir = Path(__file__).resolve().parents[2]
        auth_file = base_dir / "client-ui" / "src" / "lib" / "auth.ts"
        self.assertTrue(auth_file.exists())
        content = auth_file.read_text(encoding="utf-8")
        self.assertIn("login_url", content)

    def test_client_ui_template_routes(self):
        base_dir = Path(__file__).resolve().parents[2]
        template = base_dir / "aist" / "templates" / "aist" / "client_portal.html"
        self.assertTrue(template.exists())
        content = template.read_text(encoding="utf-8")
        self.assertIn("__AIST_ROUTES__", content)
