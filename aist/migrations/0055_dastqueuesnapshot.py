# Written by hand to match what makemigrations produces for DastQueueSnapshot;
# `manage.py makemigrations --check` detects no further change.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0054_dasttestbinding"),
    ]

    operations = [
        migrations.CreateModel(
            name="DastQueueSnapshot",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("run_id", models.CharField(blank=True, default="", max_length=255)),
                ("descriptor", models.JSONField(blank=True, default=dict)),
                ("page", models.JSONField(blank=True, default=dict)),
                ("etag", models.CharField(blank=True, default="", max_length=128)),
                ("descriptor_digest", models.CharField(blank=True, default="", max_length=128)),
                ("unavailable_code", models.CharField(blank=True, default="", max_length=64)),
                ("unavailable_detail", models.TextField(blank=True, default="")),
                ("source", models.CharField(default="gateway", max_length=32)),
                ("fetched_at", models.DateTimeField(blank=True, null=True)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "pipeline",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="dast_queue_snapshot",
                        to="aist.aistpipeline",
                    ),
                ),
            ],
        ),
    ]
