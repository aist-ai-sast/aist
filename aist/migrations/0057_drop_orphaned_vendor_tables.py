from django.db import migrations

# Tables DefectDojo 3.x left behind without a model. dojo.0265 and dojo.0266 drop the
# Stub Finding and Credential Manager models from state with no database_operations, and
# removing social-auth-app-django from the dependencies orphans its tables the same way.
#
# Django builds TRUNCATE from installed models, so an orphan is left out of it while its
# foreign keys into live tables remain, and flushing the test database fails.
#
# The drop is irreversible and forecloses a downgrade to a release that still defines
# these models; the guard below refuses to run while any row is present, so nothing is
# dropped silently.
ORPHANED_TABLES = (
    "dojo_cred_user_notes",
    "dojo_cred_mapping",
    "dojo_cred_userevent",
    "dojo_cred_user",
    "dojo_stub_finding",
    "social_auth_usersocialauth",
    "social_auth_partial",
    "social_auth_nonce",
    "social_auth_code",
    "social_auth_association",
)

DROP_ORPHANED_TABLES = "\n".join(
    f"DROP TABLE IF EXISTS {table} CASCADE;" for table in ORPHANED_TABLES
)


def refuse_when_rows_remain(apps, schema_editor):
    """Abort rather than drop rows an operator may still need."""
    connection = schema_editor.connection
    present = set(connection.introspection.table_names())
    populated = {}
    with connection.cursor() as cursor:
        for table in ORPHANED_TABLES:
            if table not in present:
                continue
            # Table names come from the module constant above. The lock holds until the
            # enclosing migration transaction ends, so no row can be committed between
            # this count and the DROP.
            cursor.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
            cursor.execute(f"SELECT count(*) FROM {table}")
            count = cursor.fetchone()[0]
            if count:
                populated[table] = count
    if populated:
        rows = ", ".join(f"{table}={count}" for table, count in sorted(populated.items()))
        msg = (
            f"Refusing to drop vendor tables that still hold rows ({rows}). DefectDojo 3.x "
            "removed the features behind them, so the rows are no longer reachable through "
            "any endpoint or page. Export or delete them, then run this migration again."
        )
        raise RuntimeError(msg)


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0056_enable_product_tag_inheritance"),
        ("dojo", "0266_remove_credential_manager"),
    ]

    operations = [
        migrations.RunPython(
            refuse_when_rows_remain,
            reverse_code=migrations.RunPython.noop,
        ),
        # Reversing restores neither the tables nor their rows. It is a no-op rather than
        # an error so that migrating backwards past this point still completes; a release
        # that defines these models again would need them recreated by hand.
        migrations.RunSQL(sql=DROP_ORPHANED_TABLES, reverse_sql=migrations.RunSQL.noop),
    ]
