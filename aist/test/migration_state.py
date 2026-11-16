"""Historical AIST model state, pinned to the vendor schema actually in the database."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


@dataclass(frozen=True, slots=True)
class HistoricalScanOwner:

    """The rows a historical scan hangs off: its reporter, AIST project and branch version, engagement and test type."""

    user: Any
    project: Any
    version: Any
    engagement: Any
    test_type: Any


def seed_historical_organization(old_apps, label: str):
    """Create, through ``old_apps``, an organization owning a new product type named after ``label``."""
    product_type = old_apps.get_model("dojo", "Product_Type").objects.create(name=f"{label} PT")
    return old_apps.get_model("aist", "Organization").objects.create(name=f"{label} org", product_type=product_type)


def seed_historical_project(old_apps, label: str, *, product_type=None, **project_fields):
    """
    Create, through ``old_apps``, an AIST project on a new product with its own SLA.

    The product goes under ``product_type``, or under a new product type named after ``label``.
    """
    if product_type is None:
        product_type = old_apps.get_model("dojo", "Product_Type").objects.create(name=f"{label} PT")
    sla = old_apps.get_model("dojo", "SLA_Configuration").objects.create(name=f"{label} SLA")
    product = old_apps.get_model("dojo", "Product").objects.create(
        name=f"{label} product", description="", prod_type=product_type, sla_configuration=sla,
    )
    return old_apps.get_model("aist", "AISTProject").objects.create(product=product, **project_fields)


def seed_historical_version(old_apps, project, *, version: str = "main", version_type: str = "GIT_BRANCH"):
    """Create, through ``old_apps``, a source version of ``project``; a ``main`` branch unless told otherwise."""
    return old_apps.get_model("aist", "AISTProjectVersion").objects.create(
        project=project, version=version, version_type=version_type,
    )


def seed_historical_scan_owner(old_apps, label: str, *, test_type: str) -> HistoricalScanOwner:
    """Create, through ``old_apps``, a reporter, an AIST project on branch ``main`` and a scan engagement."""
    now = timezone.now()
    project = seed_historical_project(old_apps, label)
    return HistoricalScanOwner(
        user=old_apps.get_model("auth", "User").objects.create(username=label),
        project=project,
        version=seed_historical_version(old_apps, project),
        engagement=old_apps.get_model("dojo", "Engagement").objects.create(
            name=label, product=project.product, target_start=now, target_end=now,
        ),
        test_type=old_apps.get_model("dojo", "Test_Type").objects.create(name=test_type),
    )


def seed_historical_test(old_apps, owner: HistoricalScanOwner):
    """Create, through ``old_apps``, one scan result under ``owner``'s engagement and test type."""
    now = timezone.now()
    return old_apps.get_model("dojo", "Test").objects.create(
        engagement=owner.engagement, test_type=owner.test_type, target_start=now, target_end=now,
    )


def seed_historical_finding(old_apps, test, reporter, *, title: str, hash_code: str, **fields):
    """Create, through ``old_apps``, a Medium finding of ``test`` reported by ``reporter``, dated today."""
    return old_apps.get_model("dojo", "Finding").objects.create(
        test=test, title=title, severity="Medium", numerical_severity="S2", date=timezone.now().date(),
        reporter_id=reporter.pk, hash_code=hash_code, **fields,
    )


def historical_state(executor: MigrationExecutor, targets: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """
    Extend AIST migration targets with DefectDojo's current leaf.

    A target naming only ``aist`` resolves the ``dojo`` models to whatever state that
    AIST migration depends on, while the database carries the schema of the vendor
    revision in use. Fields the vendor has since dropped then appear in the historical
    model and not in the table.
    """
    apps_named = {app for app, _ in targets}
    pinned = list(targets)
    for app, name in executor.loader.graph.leaf_nodes():
        if app not in apps_named:
            pinned.append((app, name))
    return pinned


class HistoricalMigrationTestCase(TransactionTestCase):

    """
    A test of the migrations between ``migrate_from`` and ``migrate_to``.

    ``setUp`` migrates the database to ``migrate_from``, hands that state's apps to :meth:`prepare` to seed rows,
    then migrates to ``migrate_to`` and exposes its apps as ``self.apps``; both states are pinned through
    :func:`historical_state`. ``tearDown`` migrates the database back to the graph's leaf.
    """

    migrate_from: list[tuple[str, str]]
    migrate_to: list[tuple[str, str]]

    def prepare(self, old_apps) -> None:
        """Seed the rows the migration under test reads, through ``old_apps``."""

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        pinned_from = historical_state(executor, self.migrate_from)
        executor.migrate(pinned_from)
        self.prepare(executor.loader.project_state(pinned_from).apps)

        executor = MigrationExecutor(connection)
        pinned_to = historical_state(executor, self.migrate_to)
        executor.migrate(pinned_to)
        self.apps = executor.loader.project_state(pinned_to).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()
