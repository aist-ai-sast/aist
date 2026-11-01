from django.db import migrations

# A DefectDojo release seeded its own upsell into the shared announcement banner, which is
# shown to every authenticated user on every page. The banner itself is a feature an
# operator may want, so only a message pointing at the vendor's commercial site is cleared.
VENDOR_MARKETING_HOST = "defectdojo.com"


def clear_vendor_marketing(apps, schema_editor):
    """Blank a banner that advertises the platform vendor, leaving operator text alone."""
    announcement = apps.get_model("dojo", "Announcement")
    announcement.objects.filter(message__icontains=VENDOR_MARKETING_HOST).update(message="")


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0057_drop_orphaned_vendor_tables"),
        ("dojo", "0266_remove_credential_manager"),
    ]

    operations = [
        # Reversing would mean restoring an advertisement; there is nothing to put back.
        migrations.RunPython(clear_vendor_marketing, reverse_code=migrations.RunPython.noop),
    ]
