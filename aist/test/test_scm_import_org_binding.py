"""SCM import dialogs preserve organization identifiers and escape their displayed names."""
from django.urls import reverse
from lxml import html

from aist.models import Organization
from aist.test.test_api import AISTApiBase


class ScmImportOrganizationBindingTests(AISTApiBase):
    def test_import_dialogs_render_organization_names_as_text(self):
        organization = Organization.objects.create(
            name='<img src=x onerror="alert(1)">', product_type=self.prod_type,
        )
        self.login_as_superuser()

        response = self.client.get(reverse("aist:aist_project_list"))

        self.assertEqual(response.status_code, 200)
        page = html.fromstring(response.content)
        for provider in ("gitlab", "gerrit", "gitea", "github"):
            with self.subTest(provider=provider):
                select = page.get_element_by_id(f"{provider}-org-select")
                self.assertEqual(
                    [(option.get("value"), option.text_content()) for option in select.iter("option") if option.get("value")],
                    [(str(organization.pk), organization.name)],
                )
                self.assertEqual(list(select.iter("img")), [])
