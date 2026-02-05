from __future__ import annotations

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from aist.ai_filter import apply_ai_filter
from dojo.models import Engagement, Finding, Product, Product_Type, SLA_Configuration, Test, Test_Type


class AIFilterOrderingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="orderer",
            email="orderer@example.com",
            password="pass",  # noqa: S106
        )
        self.sla = SLA_Configuration.objects.create(name="SLA ordering")
        self.prod_type = Product_Type.objects.create(name="PT ordering")
        self.product = Product.objects.create(
            name="Order Product",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )
        self.engagement = Engagement.objects.create(
            name="Engage",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        self.test_type = Test_Type.objects.create(name="Semgrep")
        self.test = Test.objects.create(
            engagement=self.engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=self.test_type,
        )

    def test_default_ordering_by_severity(self):
        severities = ["Low", "Critical", "Medium", "High", "Info"]
        for sev in severities:
            Finding.objects.create(
                test=self.test,
                title=f"{sev} finding",
                severity=sev,
                date=timezone.now(),
                reporter=self.user,
            )

        qs = Finding.objects.filter(test=self.test)
        filtered = apply_ai_filter(
            qs,
            {"limit": 10, "severity": [{"comparison": "EXISTS", "value": True}]},
        )

        ordered = list(filtered.values_list("severity", flat=True))
        self.assertEqual(ordered, ["Critical", "High", "Medium", "Low", "Info"])

    def test_custom_ordering_by_date(self):
        base = date.today()
        for offset, sev in enumerate(["High", "Low", "Medium"]):
            Finding.objects.create(
                test=self.test,
                title=f"{sev} finding",
                severity=sev,
                date=base + timedelta(days=offset),
                reporter=self.user,
            )

        qs = Finding.objects.filter(test=self.test)
        filtered = apply_ai_filter(
            qs,
            {
                "limit": 10,
                "severity": [{"comparison": "EXISTS", "value": True}],
                "order_by": [{"field": "date", "direction": "ASC"}],
            },
        )

        ordered_dates = list(filtered.values_list("date", flat=True))
        self.assertEqual(ordered_dates, sorted(ordered_dates))
