from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0045_drop_dast_binding_autonomous_policy"),
    ]

    operations = [
        # Widening a varchar in PostgreSQL is a catalog-only change: no table rewrite, no
        # index rebuild. The width follows DastTarget.provider_id, which a DAST_TARGET
        # version carries verbatim so operators recognise it in the findings list.
        migrations.AlterField(
            model_name="aistprojectversion",
            name="version",
            field=models.CharField(db_index=True, max_length=255),
        ),
        migrations.AlterField(
            model_name="aistprojectversion",
            name="version_type",
            field=models.CharField(
                choices=[
                    ("GIT_BRANCH", "Git branch"),
                    ("GIT_HASH", "Git commit/hash"),
                    ("FILE_HASH", "File hash (uploaded archive)"),
                    ("DAST_TARGET", "DAST target (no source revision)"),
                ],
                db_index=True,
                default="GIT_BRANCH",
                max_length=16,
            ),
        ),
    ]
