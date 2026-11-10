"""Relocation preserves source paths and finding identity without retaining an origin."""
from importlib import import_module

from django.db import connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.utils import timezone

from aist.test.migration_state import HistoricalMigrationTestCase


class RelativeSourceLinksMigrationTests(HistoricalMigrationTestCase):
    migrate_from = [("aist", "0060_checkout_relative_file_uri_paths")]
    migrate_to = [("aist", "0061_relative_source_links")]

    def prepare(self, old_apps):
        user = old_apps.get_model("auth", "User").objects.create(username="source-migration")
        product_type = old_apps.get_model("dojo", "Product_Type").objects.create(name="Source links")
        product = old_apps.get_model("dojo", "Product").objects.create(name="Sources", prod_type=product_type)
        engagement = old_apps.get_model("dojo", "Engagement").objects.create(
            name="scan", product=product, target_start=timezone.now(), target_end=timezone.now(),
        )
        test_type = old_apps.get_model("dojo", "Test_Type").objects.create(name="Source scan")
        test = old_apps.get_model("dojo", "Test").objects.create(
            engagement=engagement, test_type=test_type, target_start=timezone.now(), target_end=timezone.now(),
        )
        finding = old_apps.get_model("dojo", "Finding").objects.create(
            test=test, title="Source finding", severity="Medium", numerical_severity="S2",
            date=timezone.now().date(), reporter=user, hash_code="unchanged-hash", file_path="src/[slug]/naïve.py",
        )
        self.finding_id = finding.pk
        self.path = "/api/v2/aist/projects_version/42/files/blob/src/%5Bslug%5D/na%C3%AFve.py"
        metadata = old_apps.get_model("dojo", "DojoMeta").objects
        self.ids = []
        for value in ("https://old.example" + self.path, "http://old.example:8080" + self.path + "?download=1#L4", self.path):
            finding.pk = None
            finding.save()
            self.ids.append(metadata.create(finding=finding, name="sourcefile_link", value=value).pk)
        self.finding_id = finding.pk
        self.other_id = metadata.create(finding=finding, name="reference", value="https://old.example").pk

    def test_relocation_keeps_encoded_paths_and_finding_identity(self):
        metadata = self.apps.get_model("dojo", "DojoMeta").objects
        self.assertEqual(list(metadata.filter(pk__in=self.ids).values_list("value", flat=True)), [self.path] * 3)
        self.assertEqual(metadata.get(pk=self.other_id).value, "https://old.example")
        finding = self.apps.get_model("dojo", "Finding").objects.get(pk=self.finding_id)
        self.assertEqual((finding.hash_code, finding.file_path), ("unchanged-hash", "src/[slug]/naïve.py"))

    def test_repeated_conversion_is_idempotent_and_unknown_links_fail_atomically(self):
        migration = import_module("aist.migrations.0061_relative_source_links")
        metadata = self.apps.get_model("dojo", "DojoMeta").objects
        with connection.schema_editor() as editor:
            migration.relative_source_links(self.apps, editor)
        self.assertEqual(metadata.filter(pk__in=self.ids, value=self.path).count(), 3)
        bad = metadata.get(pk=self.ids[0])
        bad.value = "https://unknown.example/file"
        bad.save(update_fields=["value"])
        try:
            with self.assertRaisesMessage(ValueError, f"metadata id={bad.pk}"), transaction.atomic(), connection.schema_editor() as editor:
                migration.relative_source_links(self.apps, editor)
        finally:
            # Let the historical test's tearDown restore the current schema normally.
            bad.value = self.path
            bad.save(update_fields=["value"])

    def test_reverse_refuses_to_claim_origins_can_be_restored(self):
        migration = import_module("aist.migrations.0061_relative_source_links")
        with self.assertRaisesMessage(IrreversibleError, "restore the pre-upgrade"), connection.schema_editor() as editor:
            migration.refuse_lossy_reverse(self.apps, editor)

    def test_invalid_link_after_a_written_batch_rolls_back_all_changes(self):
        migration = import_module("aist.migrations.0061_relative_source_links")
        metadata = self.apps.get_model("dojo", "DojoMeta").objects
        finding_model = self.apps.get_model("dojo", "Finding")
        template = finding_model.objects.get(pk=self.finding_id)
        row_ids = []
        for index in range(501):
            template.pk = None
            template.save()
            value = "https://old.example" + self.path if index < 500 else "https://unknown.example/file"
            row_ids.append(metadata.create(finding=template, name="sourcefile_link", value=value).pk)
        with self.assertRaisesMessage(ValueError, "Unknown sourcefile_link"), transaction.atomic(), connection.schema_editor() as editor:
            migration.relative_source_links(self.apps, editor)
        self.assertEqual(metadata.filter(pk__in=row_ids, value="https://old.example" + self.path).count(), 500)
        metadata.filter(pk=row_ids[-1]).update(value=self.path)
