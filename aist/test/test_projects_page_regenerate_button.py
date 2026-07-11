from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.models import AISTProject


class ProjectsPageRegenerateButtonTests(TestCase):

    """The legacy projects page must expose a per-row regenerate-analysis URL and button."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="regen_page_admin",
            password="pass",  # noqa: S106
            email="regen_page_admin@example.com",
        )
        self.client.force_login(self.user)

        sla = SLA_Configuration.objects.create(name="SLA Regen Page")
        prod_type = Product_Type.objects.create(name="PT Regen Page")
        product = Product.objects.create(
            name="Regen Page Product",
            description="desc",
            prod_type=prod_type,
            sla_configuration_id=sla.id,
        )
        self.project = AISTProject.objects.create(
            product=product,
            supported_languages=["python"],
            compilable=False,
            profile={},
        )

    def test_page_renders_regenerate_button_and_data_url(self):
        response = self.client.get(reverse("aist:aist_project_list"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('id="aist-project-regenerate-btn"', content)

        expected_url = reverse("aist_api:project_regenerate_analysis", kwargs={"project_id": self.project.id})
        self.assertIn(f'data-regenerate-url="{expected_url}"', content)
