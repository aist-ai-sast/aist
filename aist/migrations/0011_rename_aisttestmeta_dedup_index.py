from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0010_reset_system_url_prefix"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="aisttestmeta",
            old_name="aist_aistt_dedup_0a28a4_idx",
            new_name="aist_testmeta_dedup_idx",
        ),
    ]
