from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0029_org_integration_vpn_link"),
    ]

    operations = [
        migrations.AddField(
            model_name="projectintegrationoverride",
            name="is_disabled",
            field=models.BooleanField(
                default=False,
                help_text="When True, the org-level integration of this type is disabled for this project.",
            ),
        ),
    ]
