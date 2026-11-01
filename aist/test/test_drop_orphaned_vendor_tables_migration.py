"""
The orphaned-table drop refuses to destroy rows an operator may still need.

DefectDojo 3.x removed the models behind these tables without dropping them, so the rows
survive an upgrade and are reachable only through the database. The migration's guard is
the only thing between that data and a ``DROP ... CASCADE``, and it runs against tables no
model describes, so no model-level test would exercise it.
"""
from __future__ import annotations

import importlib

from django.db import connection, transaction
from django.test import TransactionTestCase

_migration = importlib.import_module("aist.migrations.0057_drop_orphaned_vendor_tables")
ORPHANED_TABLES = _migration.ORPHANED_TABLES
refuse_when_rows_remain = _migration.refuse_when_rows_remain

STAND_IN = "dojo_cred_user"


class DropOrphanedVendorTablesGuardTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.assertIn(STAND_IN, ORPHANED_TABLES)
        self._drop_stand_in()

    def tearDown(self):
        self._drop_stand_in()
        super().tearDown()

    def _drop_stand_in(self):
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS {STAND_IN} CASCADE")

    def _recreate_stand_in(self, *, rows: int):
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE TABLE {STAND_IN} (id serial PRIMARY KEY, name text)")
            for index in range(rows):
                cursor.execute(f"INSERT INTO {STAND_IN} (name) VALUES (%s)", [f"row-{index}"])

    def _table_exists(self) -> bool:
        return STAND_IN in set(connection.introspection.table_names())

    def _run_guard(self):
        # The migration runs inside a transaction; the guard's LOCK TABLE requires one.
        with transaction.atomic(), connection.schema_editor() as schema_editor:
            refuse_when_rows_remain(None, schema_editor)

    def test_a_populated_table_stops_the_migration_and_survives(self):
        self._recreate_stand_in(rows=2)

        with self.assertRaises(RuntimeError) as caught:
            self._run_guard()

        self.assertIn(STAND_IN, str(caught.exception))
        self.assertIn("2", str(caught.exception))
        self.assertTrue(self._table_exists(), "the guard must not drop what it refused")

    def test_an_empty_table_does_not_stop_the_migration(self):
        self._recreate_stand_in(rows=0)

        self._run_guard()

    def test_a_table_already_gone_does_not_stop_the_migration(self):
        self.assertFalse(self._table_exists())

        self._run_guard()
