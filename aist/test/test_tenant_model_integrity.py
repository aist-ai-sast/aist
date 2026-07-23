from django.contrib.auth import get_user_model
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import IntegrityError
from django.test import TransactionTestCase
from django.utils import timezone
from dojo.models import Engagement, Finding, Product, Product_Type, SLA_Configuration, Test, Test_Type

from aist.models import (
    AISTProject,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    ProjectIntegrationOverride,
    WorkItemLink,
    WorkItemProvider,
    WorkItemProviderType,
)


class TenantModelIntegrityTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="tenant-integrity-user",
            email="tenant-integrity@example.com",
        )
        self.sla = SLA_Configuration.objects.create(name="Tenant integrity SLA")
        self.test_type = Test_Type.objects.create(name="Tenant integrity scan")

        self.product_type_a = Product_Type.objects.create(name="Tenant integrity PT A")
        self.product_type_b = Product_Type.objects.create(name="Tenant integrity PT B")
        self.organization_a = Organization.objects.create(
            name="Tenant integrity Org A",
            product_type=self.product_type_a,
        )
        self.organization_b = Organization.objects.create(
            name="Tenant integrity Org B",
            product_type=self.product_type_b,
        )
        self.product_a = self._product("Tenant integrity Product A", self.product_type_a)
        self.product_b = self._product("Tenant integrity Product B", self.product_type_b)
        self.project_a = AISTProject.objects.create(product=self.product_a)
        self.project_b = AISTProject.objects.create(product=self.product_b)
        self.finding_a = self._finding("Tenant integrity Finding A", self.product_a)
        self.finding_b = self._finding("Tenant integrity Finding B", self.product_b)

    def _product(self, name, product_type):
        return Product.objects.create(
            name=name,
            description="",
            prod_type=product_type,
            sla_configuration=self.sla,
        )

    def _finding(self, name, product):
        engagement = Engagement.objects.create(
            name=f"{name} engagement",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=product,
        )
        test = Test.objects.create(
            engagement=engagement,
            test_type=self.test_type,
            target_start=timezone.now(),
            target_end=timezone.now(),
        )
        return Finding.objects.create(
            test=test,
            title=name,
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )

    def test_project_organization_is_derived_from_product_type(self):
        self.assertEqual(self.project_a.organization_id, self.organization_a.id)
        self.assertEqual(self.project_a.organization, self.organization_a)

        extra_product = self._product("Tenant integrity derived Product", self.product_type_a)
        unsaved_project = AISTProject(product=extra_product)
        self.assertEqual(unsaved_project.organization_id, self.organization_a.id)

    def test_project_has_no_independent_organization_field(self):
        with self.assertRaises(FieldDoesNotExist):
            AISTProject._meta.get_field("organization")

    def test_project_integration_override_cannot_cross_tenant_or_type(self):
        integration_b = OrgIntegration.objects.create(
            organization=self.organization_b,
            integration_type=OrgIntegrationType.GITHUB,
            name="Tenant integrity GitHub B",
        )
        with self.assertRaises(IntegrityError):
            ProjectIntegrationOverride.objects.create(
                project=self.project_a,
                integration_type=OrgIntegrationType.GITHUB,
                org_integration=integration_b,
            )

        vpn_a = OrgIntegration.objects.create(
            organization=self.organization_a,
            integration_type=OrgIntegrationType.VPN,
            name="Tenant integrity VPN A",
        )
        with self.assertRaises(IntegrityError):
            ProjectIntegrationOverride.objects.create(
                project=self.project_a,
                integration_type=OrgIntegrationType.GITHUB,
                org_integration=vpn_a,
            )

    def test_vpn_references_cannot_cross_tenant(self):
        vpn_b = OrgIntegration.objects.create(
            organization=self.organization_b,
            integration_type=OrgIntegrationType.VPN,
            name="Tenant integrity VPN B",
        )
        with self.assertRaises(IntegrityError):
            OrgIntegration.objects.create(
                organization=self.organization_a,
                integration_type=OrgIntegrationType.GITHUB,
                name="Tenant integrity invalid routed integration",
                vpn_integration=vpn_b,
            )
        with self.assertRaises(IntegrityError):
            WorkItemProvider.objects.create(
                organization=self.organization_a,
                provider_type=WorkItemProviderType.JIRA,
                name="Tenant integrity invalid routed provider",
                vpn_integration=vpn_b,
            )

    def test_provider_backed_link_cannot_cross_tenant(self):
        provider_b = WorkItemProvider.objects.create(
            organization=self.organization_b,
            provider_type=WorkItemProviderType.JIRA,
            name="Tenant integrity provider B",
        )
        invalid_link = WorkItemLink(
            finding=self.finding_a,
            provider=provider_b,
            external_url="https://issues.example.test/B-1",
        )
        with self.assertRaises(ValidationError):
            invalid_link.full_clean()
        with self.assertRaises(IntegrityError):
            WorkItemLink.objects.create(
                finding=self.finding_a,
                provider=provider_b,
                external_url="https://issues.example.test/B-1",
            )

    def test_linked_finding_tenant_path_cannot_be_reassigned(self):
        provider_a = WorkItemProvider.objects.create(
            organization=self.organization_a,
            provider_type=WorkItemProviderType.JIRA,
            name="Tenant integrity provider A",
        )
        WorkItemLink.objects.create(
            finding=self.finding_a,
            provider=provider_a,
            external_url="https://issues.example.test/A-1",
        )
        with self.assertRaises(IntegrityError):
            Finding.objects.filter(pk=self.finding_a.pk).update(test=self.finding_b.test)

    def test_parent_ownership_cannot_invalidate_existing_relations(self):
        with self.assertRaises(IntegrityError):
            Product.objects.filter(pk=self.product_a.pk).update(prod_type=self.product_type_b)

        replacement_product = self._product(
            "Tenant integrity replacement Product",
            self.product_type_b,
        )
        with self.assertRaises(IntegrityError):
            AISTProject.objects.filter(pk=self.project_a.pk).update(product=replacement_product)

        unused_product_type = Product_Type.objects.create(name="Tenant integrity unused PT")
        with self.assertRaises(IntegrityError):
            Organization.objects.filter(pk=self.organization_a.pk).update(product_type=unused_product_type)
        with self.assertRaises(IntegrityError):
            self.organization_a.delete()

        integration_a = OrgIntegration.objects.create(
            organization=self.organization_a,
            integration_type=OrgIntegrationType.GITHUB,
            name="Tenant integrity immutable integration",
        )
        with self.assertRaises(IntegrityError):
            OrgIntegration.objects.filter(pk=integration_a.pk).update(organization=self.organization_b)
