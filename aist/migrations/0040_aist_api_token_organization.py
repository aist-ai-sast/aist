import django.db.models.deletion
from django.db import migrations, models


def revoke_legacy_tokens(apps, schema_editor):
    # Existing PATs were user-wide and cannot be assigned to one tenant safely.
    # Force rotation instead of guessing an organization and preserving excess access.
    apps.get_model("aist", "AISTApiToken").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("aist", "0039_dast_integration")]

    operations = [
        migrations.AddField(
            model_name="aistapitoken",
            name="organization",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="api_tokens",
                to="aist.organization",
            ),
        ),
        migrations.RunPython(revoke_legacy_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="aistapitoken",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="api_tokens",
                to="aist.organization",
            ),
        ),
        migrations.AlterUniqueTogether(
            name="aistapitoken",
            unique_together={("user", "organization", "name")},
        ),
        migrations.AddIndex(
            model_name="aistapitoken",
            index=models.Index(fields=["organization"], name="aist_api_token_org_idx"),
        ),
    ]
