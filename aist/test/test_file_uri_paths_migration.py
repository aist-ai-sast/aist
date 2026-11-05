"""Migration 0060 rewrites stored ``file:`` URI finding paths relative to the checkout their pipeline scanned."""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from django.utils import timezone

from aist.link_builder import SOURCEFILE_LINK_META_NAME, LinkBuilder
from aist.test.migration_state import HistoricalMigrationTestCase

CHECKOUT_ROOT = "/workspace/storefront"
# The host the enrichment worker's PUBLIC_BASE_URL named, unlike the one migrate runs with.
LINK_HOST = "aist.example.test"


class FileUriPathsMigrationTests(HistoricalMigrationTestCase):
    migrate_from = [("aist", "0059_admissible_stored_launch_settings")]
    migrate_to = [("aist", "0060_checkout_relative_file_uri_paths")]

    def prepare(self, old_apps):
        user = old_apps.get_model("auth", "User").objects.create(username="file-uri-migration")
        product_type = old_apps.get_model("dojo", "Product_Type").objects.create(name="File URI migration PT")
        sla = old_apps.get_model("dojo", "SLA_Configuration").objects.create(name="File URI migration SLA")
        product = old_apps.get_model("dojo", "Product").objects.create(
            name="File URI migration product", description="", prod_type=product_type, sla_configuration=sla,
        )
        project = old_apps.get_model("aist", "AISTProject").objects.create(product=product)
        version = old_apps.get_model("aist", "AISTProjectVersion").objects.create(
            project=project, version="main", version_type="GIT_BRANCH",
        )
        self.version_id = version.pk
        engagement = old_apps.get_model("dojo", "Engagement").objects.create(
            name="eslint_security", product=product, target_start=timezone.now(), target_end=timezone.now(),
        )
        test_type = old_apps.get_model("dojo", "Test_Type").objects.create(name="ESLint Scan (SARIF)")
        test_model = old_apps.get_model("dojo", "Test")
        pipeline_model = old_apps.get_model("aist", "AISTPipeline")
        finding_model = old_apps.get_model("dojo", "Finding")
        dojo_meta_model = old_apps.get_model("dojo", "DojoMeta")

        def scan(*launch_data):
            test = test_model.objects.create(
                engagement=engagement, test_type=test_type, target_start=timezone.now(), target_end=timezone.now(),
            )
            for index, data in enumerate(launch_data):
                pipeline = pipeline_model.objects.create(
                    id=f"scan-{test.pk}-{index}", project=project, project_version=version, status="FINISHED",
                    launch_data=data,
                )
                pipeline.tests.add(test)
            return test

        def finding(test, file_path, *, linked_subpath=None):
            pk = finding_model.objects.create(
                test=test, title="Generic Object Injection Sink", severity="Medium", numerical_severity="S2",
                date=timezone.now().date(), reporter_id=user.pk, file_path=file_path, hash_code="import-time-hash",
            ).pk
            if linked_subpath is not None:
                dojo_meta_model.objects.create(
                    finding_id=pk,
                    name=SOURCEFILE_LINK_META_NAME,
                    value=urlunsplit(
                        urlsplit(LinkBuilder.build_project_version_file_blob(version.pk, linked_subpath))._replace(
                            netloc=LINK_HOST,
                        ),
                    ),
                )
            return pk

        scanned = scan({"trim_path": CHECKOUT_ROOT})
        self.uri_in_checkout = finding(
            scanned,
            f"file://{CHECKOUT_ROOT}/src/app/%5Bslug%5D/page.tsx",
            # A link built from the still-encoded subpath.
            linked_subpath="src/app/%5Bslug%5D/page.tsx",
        )
        self.single_slash_uri = finding(scanned, f"file:{CHECKOUT_ROOT}/src/app/layout.tsx")
        self.uri_outside_checkout = finding(scanned, "file:///opt/eslint/node_modules/shim.js")
        self.plain_absolute = finding(scanned, f"{CHECKOUT_ROOT}/src/app/layout.tsx")
        self.uri_without_known_root = finding(scan({}), f"file://{CHECKOUT_ROOT}/src/app/page.tsx")
        self.uri_with_conflicting_roots = finding(
            scan({"trim_path": CHECKOUT_ROOT}, {"trim_path": "/workspace/other"}),
            f"file://{CHECKOUT_ROOT}/src/app/page.tsx",
        )

    def _stored_path(self, pk):
        return self.apps.get_model("dojo", "Finding").objects.get(pk=pk).file_path

    def test_a_file_uri_inside_the_scanned_checkout_becomes_relative_and_keeps_its_hash(self):
        stored = self.apps.get_model("dojo", "Finding").objects.values("file_path", "hash_code").get(
            pk=self.uri_in_checkout,
        )
        self.assertEqual(stored, {"file_path": "src/app/[slug]/page.tsx", "hash_code": "import-time-hash"})

    def test_the_source_link_of_a_rewritten_finding_opens_its_new_path(self):
        link = self.apps.get_model("dojo", "DojoMeta").objects.get(
            finding_id=self.uri_in_checkout, name=SOURCEFILE_LINK_META_NAME,
        ).value
        self.assertEqual(LinkBuilder.parse(link), (self.version_id, "src/app/[slug]/page.tsx"))
        self.assertEqual(urlsplit(link).netloc, LINK_HOST)

    def test_a_single_slash_file_uri_becomes_relative(self):
        self.assertEqual(self._stored_path(self.single_slash_uri), "src/app/layout.tsx")

    def test_a_file_uri_outside_the_checkout_becomes_its_local_path(self):
        self.assertEqual(self._stored_path(self.uri_outside_checkout), "/opt/eslint/node_modules/shim.js")

    def test_a_path_that_is_not_a_file_uri_is_left_as_it_was(self):
        self.assertEqual(self._stored_path(self.plain_absolute), f"{CHECKOUT_ROOT}/src/app/layout.tsx")

    def test_a_finding_whose_pipeline_recorded_no_checkout_root_is_left_as_it_was(self):
        self.assertEqual(self._stored_path(self.uri_without_known_root), f"file://{CHECKOUT_ROOT}/src/app/page.tsx")

    def test_a_finding_whose_pipelines_disagree_on_the_checkout_root_is_left_as_it_was(self):
        self.assertEqual(
            self._stored_path(self.uri_with_conflicting_roots), f"file://{CHECKOUT_ROOT}/src/app/page.tsx",
        )
