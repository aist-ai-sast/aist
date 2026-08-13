from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0044_dast_sourceless_scenarios"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="dastprojectbinding",
            name="autonomous_enabled",
        ),
    ]
