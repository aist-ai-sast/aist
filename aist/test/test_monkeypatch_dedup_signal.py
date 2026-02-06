from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.utils import timezone
from dojo.finding.deduplication import do_dedupe_finding
from dojo.models import (
    Engagement,
    Finding,
    Product,
    Product_Type,
    SLA_Configuration,
    System_Settings,
    Test,
    Test_Type,
)

from aist.models import ProcessedFinding
from aist.monkeypatch import install_deduplication_monkeypatch


class MonkeypatchDedupSignalTest(TransactionTestCase):
    def setUp(self):
        System_Settings.objects.all().delete()
        System_Settings.objects.create(enable_deduplication=True)

        User = get_user_model()
        self.user = User.objects.create(
            username="tester",
            email="tester@example.com",
            password="x",  # noqa: S106
        )
        self.sla = SLA_Configuration.objects.create(
            name="SLA default for tests",
        )
        self.prod_type = Product_Type.objects.create(name="PT for tests")
        product = Product.objects.create(
            name="Test Product", description="desc", prod_type=self.prod_type, sla_configuration_id=self.sla.id,
        )
        engagement = Engagement.objects.create(
            name="Engage",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=product,
        )
        test_type = Test_Type.objects.create(name="SAST")
        self.test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )

    def test_deduplication_signal_emits_processed_finding(self):
        install_deduplication_monkeypatch()

        finding = Finding.objects.create(
            test=self.test,
            title="Test finding",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )

        do_dedupe_finding(finding)

        self.assertTrue(
            ProcessedFinding.objects.filter(test=self.test, finding=finding).exists(),
        )
