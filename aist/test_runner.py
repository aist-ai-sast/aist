"""Test runner that flushes with ``TRUNCATE ... CASCADE`` and keeps the deployment's SLA default."""
from __future__ import annotations

from django.core.management import call_command
from django.db import connections
from django.test import TransactionTestCase
from django.test.runner import DiscoverRunner

# AIST creates a Product without naming an SLA configuration (see aist/api/projects.py and
# aist/internal_upload.py), so Product.sla_configuration's default of 1 must resolve. A
# deployment gets that row from dojo/fixtures/sla_configurations.json at first boot; a test
# database is built from migrations alone, and a flush removes it again. Declaring it as a
# fixture lets Django reload it after every flush and reset the sequence past it.
DEFAULT_FIXTURES = ["default_sla_configuration"]


def _fixture_teardown_with_cascade(self):
    """
    Django's own teardown, with cascade on.

    DefectDojo declares its membership tables (``dojo_role``, ``dojo_product_member``,
    ``dojo_product_type_member``, ``dojo_product_type_group``, ``dojo_global_role``) as
    ``managed = False``, so Django leaves them out of the flush while truncating the tables
    they reference. Cascade clears them with the rest.
    """
    for db_name in self._databases_names(include_mirrors=False):
        inhibit_post_migrate = self.available_apps is not None or (
            self.serialized_rollback
            and hasattr(connections[db_name], "_test_serialized_contents")
        )
        call_command(
            "flush",
            verbosity=0,
            interactive=False,
            database=db_name,
            reset_sequences=False,
            allow_cascade=True,
            inhibit_post_migrate=inhibit_post_migrate,
        )


class AistDiscoverRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        TransactionTestCase._fixture_teardown = _fixture_teardown_with_cascade
        if not TransactionTestCase.fixtures:
            TransactionTestCase.fixtures = DEFAULT_FIXTURES
