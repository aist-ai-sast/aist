from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0050_aistpipeline_dast_binding"),
    ]

    operations = [
        migrations.AddField(
            model_name="dastrunmetadata",
            name="token_economy",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
    ]
