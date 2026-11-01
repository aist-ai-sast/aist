"""
The SLA configuration a Product falls back to exists in every environment.

``Product.sla_configuration`` defaults to pk 1, and AIST's product-creation paths
(``aist/api/projects.py``, ``aist/internal_upload.py``) never name one. A deployment loads
that row from a fixture at first boot; a test database is built from migrations alone, and a
flush removes it again, so the test runner declares it as a fixture every test case loads.
"""
from __future__ import annotations

from django.test import TestCase, TransactionTestCase
from dojo.models import Product, Product_Type, SLA_Configuration


class DefaultSlaConfigurationTests(TestCase):
    def test_a_product_can_be_created_without_naming_an_sla_configuration(self):
        product_type = Product_Type.objects.create(name="sla fallback")
        product = Product.objects.create(
            name="sla fallback", description="d", prod_type=product_type,
        )
        self.assertEqual(product.sla_configuration_id, 1)

    def test_the_default_configuration_is_present(self):
        self.assertTrue(SLA_Configuration.objects.filter(pk=1).exists())


class DefaultSlaConfigurationSurvivesAFlushTests(TransactionTestCase):

    """A ``TransactionTestCase`` flushes between tests; the fallback must come back."""

    def test_a_product_can_be_created_without_naming_an_sla_configuration(self):
        product_type = Product_Type.objects.create(name="sla fallback after flush")
        product = Product.objects.create(
            name="sla fallback after flush", description="d", prod_type=product_type,
        )
        self.assertEqual(product.sla_configuration_id, 1)

    def test_a_new_configuration_does_not_collide_with_the_default(self):
        """The fixture load resets the sequence, so the next create cannot ask for pk 1."""
        created = SLA_Configuration.objects.create(name="sla fallback sibling")
        self.assertNotEqual(created.pk, 1)
