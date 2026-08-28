from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0048_dast_execution_last_progress"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="dastexecutionstate",
            name="deadline",
        ),
        migrations.AddField(
            model_name="dastrunmetadata",
            name="delivery_quality",
            field=models.CharField(blank=True, max_length=16, null=True),
        ),
        migrations.AddField(
            model_name="dastrunmetadata",
            name="audit_state",
            field=models.CharField(blank=True, max_length=16, null=True),
        ),
        migrations.AddField(
            model_name="dastrunmetadata",
            name="findings_complete",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dastrunmetadata",
            name="source_verified",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dastrunmetadata",
            name="operator_actions_persisted",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dastrunmetadata",
            name="operator_actions",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="dastrunmetadata",
            name="operator_actions_total",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dastrunmetadata",
            name="operator_actions_truncated",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dastrunmetadata",
            name="excluded_findings",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="dastrunmetadata",
            name="excluded_findings_total",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dastrunmetadata",
            name="excluded_findings_truncated",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="dastrunmetadata",
            name="stand_id",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
