from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product, Product_Type, Product_Type_Member, Role, SLA_Configuration
from rest_framework.test import APIClient

from aist.models import AISTProject, Organization


class OrganizationCreateAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="org_api_user",
            email="org_api_user@example.com",
            password="pass",  # noqa: S106
        )
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.client.force_authenticate(user=self.user)

    def test_create_organization_creates_product_type(self):
        response = self.client.post(
            reverse("aist_api:organization_create"),
            data={"name": "Acme Org", "description": "desc"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        organization = Organization.objects.get(name="Acme Org")
        self.assertIsNotNone(organization.product_type_id)
        self.assertEqual(organization.product_type.name, "Acme Org")

    def test_create_organization_reuses_existing_product_type_by_name(self):
        existing_product_type = Product_Type.objects.create(name="Acme Existing")
        response = self.client.post(
            reverse("aist_api:organization_create"),
            data={"name": "Acme Existing"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        organization = Organization.objects.get(name="Acme Existing")
        self.assertEqual(organization.product_type_id, existing_product_type.id)

    def test_list_organizations_for_superuser_returns_all(self):
        org_one = Organization.objects.create(name="Org One")
        org_two = Organization.objects.create(name="Org Two")

        response = self.client.get(reverse("aist_api:organization_create"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        results = payload.get("results", payload)
        ids = {item["id"] for item in results}
        self.assertIn(org_one.id, ids)
        self.assertIn(org_two.id, ids)

    def test_non_privileged_user_cannot_create_organization_via_existing_product_type_name(self):
        # Attaching an Organization to a pre-existing Product_Type must require
        # Product_Type_Add just like creating a brand-new one — otherwise any
        # authenticated user could claim an orphaned Product_Type (and
        # permanently squat the Organization's unique name) with zero
        # permission check.
        Product_Type.objects.create(name="Orphaned PT")
        regular_user = get_user_model().objects.create_user(
            username="no_perms_user",
            email="no_perms_user@example.com",
            password="pass",  # noqa: S106
        )
        client = APIClient()
        client.force_authenticate(user=regular_user)

        response = client.post(
            reverse("aist_api:organization_create"),
            data={"name": "Orphaned PT"},
            format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Organization.objects.filter(name="Orphaned PT").exists())

    def test_non_privileged_user_cannot_create_brand_new_organization(self):
        regular_user = get_user_model().objects.create_user(
            username="no_perms_user2",
            email="no_perms_user2@example.com",
            password="pass",  # noqa: S106
        )
        client = APIClient()
        client.force_authenticate(user=regular_user)

        response = client.post(
            reverse("aist_api:organization_create"),
            data={"name": "Brand New Org"},
            format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Organization.objects.filter(name="Brand New Org").exists())

    def test_list_organizations_for_regular_user_returns_only_authorized(self):
        regular_user = get_user_model().objects.create_user(
            username="org_regular_user",
            email="org_regular_user@example.com",
            password="pass",  # noqa: S106
        )
        role_maintainer, _ = Role.objects.get_or_create(
            id=Roles.Maintainer,
            defaults={"name": "Maintainer"},
        )
        sla = SLA_Configuration.objects.create(name="Org API SLA")
        pt = Product_Type.objects.create(name="Org API PT")
        product = Product.objects.create(
            name="Org Product",
            description="desc",
            prod_type=pt,
            sla_configuration=sla,
        )
        Product_Type_Member.objects.create(product_type=pt, user=regular_user, role=role_maintainer)

        visible_org = Organization.objects.create(name="Visible Org", product_type=pt)
        hidden_org = Organization.objects.create(
            name="Hidden Org",
            product_type=Product_Type.objects.create(name="Hidden PT"),
        )
        AISTProject.objects.create(
            product=product,
            organization=visible_org,
            supported_languages=["python"],
            compilable=False,
            profile={},
        )

        self.client.force_authenticate(user=regular_user)
        response = self.client.get(reverse("aist_api:organization_create"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        results = payload.get("results", payload)
        names = [item["name"] for item in results]
        self.assertIn(visible_org.name, names)
        self.assertNotIn(hidden_org.name, names)
