"""Historical AIST model state, pinned to the vendor schema actually in the database."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.db.migrations.executor import MigrationExecutor


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
