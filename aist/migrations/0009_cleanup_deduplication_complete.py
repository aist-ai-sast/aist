import pgtrigger.compiler
from django.db import migrations


def _test_triggers() -> tuple[pgtrigger.compiler.Trigger, ...]:
    return (
        pgtrigger.compiler.Trigger(
            name="insert_insert",
            sql=pgtrigger.compiler.UpsertTriggerSql(
                func='INSERT INTO "dojo_testevent" ("api_scan_configuration_id", "branch_tag", "build_id", "commit_hash", "created", "description", "engagement_id", "environment_id", "id", "lead_id", "percent_complete", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "scan_type", "target_end", "target_start", "test_type_id", "title", "updated", "version") VALUES (NEW."api_scan_configuration_id", NEW."branch_tag", NEW."build_id", NEW."commit_hash", NEW."created", NEW."description", NEW."engagement_id", NEW."environment_id", NEW."id", NEW."lead_id", NEW."percent_complete", _pgh_attach_context(), NOW(), \'insert\', NEW."id", NEW."scan_type", NEW."target_end", NEW."target_start", NEW."test_type_id", NEW."title", NEW."updated", NEW."version"); RETURN NULL;',
                hash="0b6ec21ca35b61b1abcc0b2f8629cb4d1cc92930",
                operation="INSERT",
                pgid="pgtrigger_insert_insert_ecfc1",
                table="dojo_test",
                when="AFTER",
            ),
        ),
        pgtrigger.compiler.Trigger(
            name="update_update",
            sql=pgtrigger.compiler.UpsertTriggerSql(
                condition='WHEN (OLD."api_scan_configuration_id" IS DISTINCT FROM (NEW."api_scan_configuration_id") OR OLD."branch_tag" IS DISTINCT FROM (NEW."branch_tag") OR OLD."build_id" IS DISTINCT FROM (NEW."build_id") OR OLD."commit_hash" IS DISTINCT FROM (NEW."commit_hash") OR OLD."description" IS DISTINCT FROM (NEW."description") OR OLD."engagement_id" IS DISTINCT FROM (NEW."engagement_id") OR OLD."environment_id" IS DISTINCT FROM (NEW."environment_id") OR OLD."id" IS DISTINCT FROM (NEW."id") OR OLD."lead_id" IS DISTINCT FROM (NEW."lead_id") OR OLD."percent_complete" IS DISTINCT FROM (NEW."percent_complete") OR OLD."scan_type" IS DISTINCT FROM (NEW."scan_type") OR OLD."target_end" IS DISTINCT FROM (NEW."target_end") OR OLD."target_start" IS DISTINCT FROM (NEW."target_start") OR OLD."test_type_id" IS DISTINCT FROM (NEW."test_type_id") OR OLD."title" IS DISTINCT FROM (NEW."title") OR OLD."version" IS DISTINCT FROM (NEW."version"))',
                func='INSERT INTO "dojo_testevent" ("api_scan_configuration_id", "branch_tag", "build_id", "commit_hash", "created", "description", "engagement_id", "environment_id", "id", "lead_id", "percent_complete", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "scan_type", "target_end", "target_start", "test_type_id", "title", "updated", "version") VALUES (NEW."api_scan_configuration_id", NEW."branch_tag", NEW."build_id", NEW."commit_hash", NEW."created", NEW."description", NEW."engagement_id", NEW."environment_id", NEW."id", NEW."lead_id", NEW."percent_complete", _pgh_attach_context(), NOW(), \'update\', NEW."id", NEW."scan_type", NEW."target_end", NEW."target_start", NEW."test_type_id", NEW."title", NEW."updated", NEW."version"); RETURN NULL;',
                hash="777c92a16d48f7e590e50cb8fb6c0d77c9afa1b6",
                operation="UPDATE",
                pgid="pgtrigger_update_update_c40f8",
                table="dojo_test",
                when="AFTER",
            ),
        ),
        pgtrigger.compiler.Trigger(
            name="delete_delete",
            sql=pgtrigger.compiler.UpsertTriggerSql(
                func='INSERT INTO "dojo_testevent" ("api_scan_configuration_id", "branch_tag", "build_id", "commit_hash", "created", "description", "engagement_id", "environment_id", "id", "lead_id", "percent_complete", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "scan_type", "target_end", "target_start", "test_type_id", "title", "updated", "version") VALUES (OLD."api_scan_configuration_id", OLD."branch_tag", OLD."build_id", OLD."commit_hash", OLD."created", OLD."description", OLD."engagement_id", OLD."environment_id", OLD."id", OLD."lead_id", OLD."percent_complete", _pgh_attach_context(), NOW(), \'delete\', OLD."id", OLD."scan_type", OLD."target_end", OLD."target_start", OLD."test_type_id", OLD."title", OLD."updated", OLD."version"); RETURN NULL;',
                hash="51bce27193221308adc41e62f1faff5122bbbceb",
                operation="DELETE",
                pgid="pgtrigger_delete_delete_66d18",
                table="dojo_test",
                when="AFTER",
            ),
        ),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("dojo", "0259_locations"),
        ("aist", "0008_merge_20260205_1910"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[trigger.uninstall_sql for trigger in _test_triggers()],
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql=[
                'ALTER TABLE IF EXISTS "dojo_testevent" DROP COLUMN IF EXISTS "deduplication_complete";',
                'ALTER TABLE IF EXISTS "dojo_test" DROP COLUMN IF EXISTS "deduplication_complete";',
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql=[trigger.install_sql for trigger in _test_triggers()],
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
