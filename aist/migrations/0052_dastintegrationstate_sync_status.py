from django.db import migrations, models
from django.db.models import F, Q
from django.utils import timezone


def backfill_sync_status(apps, schema_editor):
    """Derive the explicit status once from the fields that implied it before this migration."""
    DastIntegrationState = apps.get_model("aist", "DastIntegrationState")
    settled = DastIntegrationState.objects.filter(sync_error_code="")
    settled.filter(capabilities_synced_at__isnull=False).update(sync_status="SUCCEEDED")
    DastIntegrationState.objects.exclude(sync_error_code="").update(sync_status="FAILED")
    # Reserved but not yet claimed by a worker.
    settled.exclude(sync_task_id="").filter(sync_claimed_at__isnull=True).update(
        sync_status="PENDING",
        sync_requested_at=timezone.now(),
    )
    # Claimed, and not finished since the claim.
    settled.filter(sync_claimed_at__isnull=False).filter(
        Q(capabilities_synced_at__isnull=True) | Q(capabilities_synced_at__lt=F("sync_claimed_at")),
    ).update(sync_status="RUNNING")


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0051_dastrunmetadata_token_economy"),
    ]

    operations = [
        migrations.AddField(
            model_name="dastintegrationstate",
            name="sync_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dastintegrationstate",
            name="sync_status",
            field=models.CharField(
                choices=[
                    ("IDLE", "Idle"),
                    ("PENDING", "Pending"),
                    ("RUNNING", "Running"),
                    ("SUCCEEDED", "Succeeded"),
                    ("FAILED", "Failed"),
                ],
                default="IDLE",
                max_length=16,
            ),
        ),
        migrations.RunPython(backfill_sync_status, migrations.RunPython.noop),
    ]
