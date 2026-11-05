"""Historical AIST model state, pinned to the vendor schema actually in the database."""
from __future__ import annotations

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


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
