from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("aist", "0024_alter_orgintegration_secret_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="aistaifindingresponse",
            name="fix",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
