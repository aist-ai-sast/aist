from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.checks import run_checks
from django.test import TestCase
from dojo.authorization.roles_permissions import Permissions, Roles
from dojo.models import Dojo_Group, Global_Role, Product, Product_Type, Role, SLA_Configuration

from aist.queries import get_authorized_aist_products

User = get_user_model()


class GlobalRoleGuardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("global-reader", "global@example.com", "pass")
        self.role, _ = Role.objects.get_or_create(id=Roles.Owner, defaults={"name": "Owner"})
        product_type = Product_Type.objects.create(name="Other tenant")
        sla = SLA_Configuration.objects.create(name="SLA")
        self.product = Product.objects.create(
            name="Other product",
            description="d",
            prod_type=product_type,
            sla_configuration=sla,
        )

    def test_global_role_does_not_bypass_aist_tenant_scope(self):
        Global_Role.objects.create(user=self.user, role=self.role)
        self.assertFalse(
            get_authorized_aist_products(Permissions.Product_View, user=self.user)
            .filter(pk=self.product.pk)
            .exists(),
        )

    def test_system_check_rejects_non_superuser_global_role(self):
        Global_Role.objects.create(user=self.user, role=self.role)
        self.assertIn("aist.E001", {error.id for error in run_checks(tags=["security"])})

    def test_system_check_allows_unassigned_non_superuser_global_role(self):
        Global_Role.objects.create(user=self.user)
        self.assertNotIn("aist.E001", {error.id for error in run_checks(tags=["security"])})

    def test_system_check_rejects_group_global_role(self):
        group = Dojo_Group.objects.create(name="global-group")
        Global_Role.objects.create(group=group, role=self.role)
        self.assertIn("aist.E001", {error.id for error in run_checks(tags=["security"])})

    def test_system_check_allows_unassigned_group_global_role(self):
        group = Dojo_Group.objects.create(name="unassigned-global-group")
        Global_Role.objects.create(group=group)
        self.assertNotIn("aist.E001", {error.id for error in run_checks(tags=["security"])})
