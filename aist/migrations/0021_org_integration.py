from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0020_workitemprovider_workitemlink"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # -----------------------------------------------------------------
        # New model: OrgIntegration
        # -----------------------------------------------------------------
        migrations.CreateModel(
            name="OrgIntegration",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("integration_type", models.CharField(
                    max_length=32,
                    choices=[
                        ("GITLAB", "GitLab"),
                        ("GITHUB", "GitHub"),
                        ("SLACK", "Slack"),
                        ("EMAIL", "Email"),
                    ],
                )),
                ("name", models.CharField(max_length=255)),
                ("config", models.JSONField(blank=True, default=dict)),
                # EncryptedCharField serialises as plain CharField in migrations.
                ("secret", models.CharField(blank=True, default="", max_length=4096)),
                ("is_active", models.BooleanField(default=True)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                )),
                ("organization", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="integrations",
                    to="aist.organization",
                )),
            ],
            options={
                "ordering": ["organization", "integration_type", "name"],
                "unique_together": {("organization", "integration_type", "name")},
            },
        ),
        # -----------------------------------------------------------------
        # New model: ProjectIntegrationOverride
        # -----------------------------------------------------------------
        migrations.CreateModel(
            name="ProjectIntegrationOverride",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("integration_type", models.CharField(
                    max_length=32,
                    choices=[
                        ("GITLAB", "GitLab"),
                        ("GITHUB", "GitHub"),
                        ("SLACK", "Slack"),
                        ("EMAIL", "Email"),
                    ],
                )),
                ("config_override", models.JSONField(blank=True, default=dict)),
                ("org_integration", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="project_overrides",
                    to="aist.orgintegration",
                )),
                ("project", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="integration_overrides",
                    to="aist.aistproject",
                )),
            ],
            options={
                "unique_together": {("project", "integration_type")},
            },
        ),
        # -----------------------------------------------------------------
        # Add org_integration FK to ScmGitlabBinding
        # (personal_access_token removed in migration 0023)
        # -----------------------------------------------------------------
        migrations.AddField(
            model_name="scmgitlabbinding",
            name="org_integration",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                limit_choices_to={"integration_type": "GITLAB"},
                related_name="+",
                to="aist.orgintegration",
            ),
        ),
        # -----------------------------------------------------------------
        # Add org_integration FK to ScmGithubBinding
        # (base_api_url removed in migration 0023)
        # -----------------------------------------------------------------
        migrations.AddField(
            model_name="scmgithubbinding",
            name="org_integration",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                limit_choices_to={"integration_type": "GITHUB"},
                related_name="+",
                to="aist.orgintegration",
            ),
        ),
    ]
