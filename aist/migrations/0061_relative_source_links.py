"""Store finding source links independently of the deployment origin."""
import re
from urllib.parse import urlsplit

from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError

SOURCE_PATH = re.compile(r"/api/v2/aist/projects_version/[0-9]+/files/blob/.+", re.DOTALL)


def relative_source_links(apps, schema_editor):
    metadata = apps.get_model("dojo", "DojoMeta").objects.using(schema_editor.connection.alias)
    changed = []
    for meta in metadata.filter(name="sourcefile_link").order_by("pk").iterator(chunk_size=500):
        try:
            parsed = urlsplit(meta.value)
        except ValueError as exc:
            msg = f"Invalid sourcefile_link metadata id={meta.pk}; repair before retrying migration."
            raise ValueError(msg) from exc
        if parsed.scheme not in {"", "http", "https"} or not SOURCE_PATH.fullmatch(parsed.path):
            msg = f"Unknown sourcefile_link metadata id={meta.pk}; repair before retrying migration."
            raise ValueError(msg)
        if meta.value != parsed.path:
            meta.value = parsed.path
            changed.append(meta)
        if len(changed) == 500:
            metadata.bulk_update(changed, ["value"], batch_size=500)
            changed.clear()
    metadata.bulk_update(changed, ["value"], batch_size=500)


def refuse_lossy_reverse(apps, schema_editor):
    metadata = apps.get_model("dojo", "DojoMeta").objects.using(schema_editor.connection.alias)
    if metadata.filter(name="sourcefile_link").exists():
        msg = "Source-link origins cannot be reconstructed; restore the pre-upgrade database backup."
        raise IrreversibleError(msg)


class Migration(migrations.Migration):
    dependencies = [("aist", "0060_checkout_relative_file_uri_paths")]
    operations = [
        # A deployment origin cannot be reconstructed after relocation. Restore the pre-upgrade
        # database backup when rolling back to an application that needs absolute links.
        migrations.RunPython(relative_source_links, reverse_code=refuse_lossy_reverse),
    ]
