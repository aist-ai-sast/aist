from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("aist", "0006_testdeduplicationprogress_last_progress_at_and_more"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="aistlaunchconfigaction",
            name="uniq_aist_launch_cfg_action",
        ),
    ]
