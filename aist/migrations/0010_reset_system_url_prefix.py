from __future__ import annotations

from django.db import migrations


def reset_system_url_prefix(apps, schema_editor):
    SystemSettings = apps.get_model("dojo", "System_Settings")
    try:
        settings = SystemSettings.objects.get()
    except SystemSettings.DoesNotExist:
        return
    if settings.url_prefix:
        settings.url_prefix = ""
        settings.save(update_fields=["url_prefix"])


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0009_cleanup_deduplication_complete"),
        ("dojo", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(reset_system_url_prefix, migrations.RunPython.noop),
    ]
