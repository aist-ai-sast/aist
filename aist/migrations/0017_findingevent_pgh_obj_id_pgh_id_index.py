"""
Add composite index on dojo_findingevent(pgh_obj_id, pgh_id DESC).

This index supports the correlated subquery in finding_history.severity_changed_events():

    SELECT severity FROM dojo_findingevent
    WHERE pgh_obj_id = $1 AND pgh_id < $2
    ORDER BY pgh_id DESC LIMIT 1

Without this index, PostgreSQL falls back to a sequential scan on pgh_obj_id
and a sort.  With it, the query uses an index-only scan with immediate LIMIT 1
termination per candidate row.

The index is created CONCURRENTLY to avoid blocking production writes during
deployment.  atomic=False is required for CONCURRENTLY operations.
"""
from django.db import migrations


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("aist", "0016_alter_aistproject_product"),
        ("dojo", "0244_pghistory_indices"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                'CREATE INDEX CONCURRENTLY IF NOT EXISTS '
                '"dojo_findingevent_pgh_obj_id_pgh_id_desc_idx" '
                'ON "dojo_findingevent" ("pgh_obj_id", "pgh_id" DESC);'
            ),
            reverse_sql=(
                'DROP INDEX CONCURRENTLY IF EXISTS '
                '"dojo_findingevent_pgh_obj_id_pgh_id_desc_idx";'
            ),
        ),
    ]
