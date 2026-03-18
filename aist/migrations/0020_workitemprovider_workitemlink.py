from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0019_aistprojectscript_and_active_script"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WorkItemProvider",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider_type", models.CharField(
                    choices=[
                        ("JIRA", "Jira"),
                        ("YOUTRACK", "YouTrack"),
                        ("GITHUB", "GitHub Issues"),
                        ("GITLAB", "GitLab Issues"),
                        ("LINEAR", "Linear"),
                        ("AZURE_DEVOPS", "Azure DevOps"),
                        ("GENERIC", "Generic (URL only)"),
                    ],
                    max_length=32,
                )),
                ("name", models.CharField(max_length=255)),
                ("base_url", models.URLField(
                    blank=True,
                    help_text="Leave blank for cloud-hosted instances (e.g. jira.atlassian.net).",
                    max_length=2048,
                )),
                ("api_token", models.CharField(blank=True, default="", max_length=2048)),
                ("provider_config", models.JSONField(blank=True, default=dict)),
                ("sync_enabled", models.BooleanField(
                    default=False,
                    help_text="Automatically sync work-item status from the tracker.",
                )),
                ("is_active", models.BooleanField(default=True)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="work_item_providers",
                    to="aist.organization",
                )),
            ],
            options={
                "ordering": ["organization", "name"],
                "unique_together": {("organization", "name")},
            },
        ),
        migrations.CreateModel(
            name="WorkItemLink",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("external_id", models.CharField(blank=True, max_length=255)),
                ("external_key", models.CharField(blank=True, max_length=255)),
                ("external_url", models.URLField(max_length=2048)),
                ("title", models.CharField(blank=True, max_length=500)),
                ("raw_status", models.CharField(blank=True, max_length=255)),
                ("status_category", models.CharField(
                    choices=[
                        ("OPEN", "Open"),
                        ("IN_PROGRESS", "In Progress"),
                        ("DONE", "Done"),
                        ("CANCELLED", "Cancelled / Won't Fix"),
                        ("UNKNOWN", "Unknown"),
                    ],
                    default="UNKNOWN",
                    max_length=16,
                )),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("sync_error", models.TextField(blank=True)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                )),
                ("finding", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="work_item_links",
                    to="dojo.finding",
                )),
                ("provider", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="work_item_links",
                    to="aist.workitemprovider",
                )),
            ],
            options={
                "unique_together": {("finding", "provider", "external_key")},
            },
        ),
        migrations.AddIndex(
            model_name="workitemlink",
            index=models.Index(fields=["finding"], name="work_item_link_finding_idx"),
        ),
        migrations.AddIndex(
            model_name="workitemlink",
            index=models.Index(
                fields=["provider", "status_category"],
                name="work_item_link_prov_status_idx",
            ),
        ),
    ]
