from __future__ import annotations

from django.urls import reverse
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product, Product_Type, Product_Type_Member, Role

from aist.models import AISTProject, Organization
from aist.test.test_api import AISTApiBase


class AISTProjectCreateAPITests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.url = reverse("aist_api:project_list")

    def test_create_empty_project_success(self):
        target_pt = Product_Type.objects.create(name="Org PT")
        role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=target_pt, user=self.user, role=role_maintainer)
        org = Organization.objects.create(name="Org A", product_type=target_pt)

        resp = self.client.post(
            self.url,
            data={
                "organization_id": org.id,
                "product_name": "New Empty Product",
                "script_path": "input_projects/default_imported_project_no_built.sh",
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data["ok"])
        project_id = resp.data["project"]["id"]
        project = AISTProject.objects.get(id=project_id)
        self.assertEqual(project.organization_id, org.id)
        self.assertEqual(project.product.name, "New Empty Product")
        self.assertEqual(project.script_path, "input_projects/default_imported_project_no_built.sh")

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

        self.assertEqual(resp.status_code, 404)
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
