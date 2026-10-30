"""
Turn product tag inheritance on platform-wide.

A product tag is copied onto that product's engagements, tests and findings as
they are saved from here on; existing rows are not rewritten. A product with
its own flag on keeps inheriting regardless of this switch.

Reversing is a no-op: the migration cannot tell a switch nobody touched from one
an operator turned off, so it writes no value back and leaves copied tags alone.
"""
from __future__ import annotations

from django.db import migrations

FIELD = "enable_product_tag_inheritance"


def enable(apps, schema_editor):
    SystemSettings = apps.get_model("dojo", "System_Settings")
    # The settings row is a singleton the platform requires; create it if absent.
    settings, _created = SystemSettings.objects.get_or_create()
    if not getattr(settings, FIELD):
        setattr(settings, FIELD, True)
        settings.save(update_fields=[FIELD])


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0055_dastqueuesnapshot"),
        ("dojo", "0188_product_enable_product_tag_inheritance_and_more"),
    ]

    operations = [
        migrations.RunPython(enable, migrations.RunPython.noop),
    ]
