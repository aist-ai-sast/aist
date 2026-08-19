from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0047_dast_run_metadata"),
    ]

    operations = [
        migrations.AddField(
            model_name="dastexecutionstate",
            name="last_progress_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
