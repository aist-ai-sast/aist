# Generated for: docs/plans/2026-07-11-gitea-integration.md

import django.db.models.deletion
from django.db import migrations, models

_SCM_TYPE_CHOICES = [
    ("GITHUB", "Github"),
    ("GITLAB", "Gitlab"),
    ("GERRIT", "Gerrit"),
    ("GITEA", "Gitea"),
]

_INTEGRATION_TYPE_CHOICES = [
    ("GITLAB", "GitLab"),
    ("GITHUB", "GitHub"),
    ("SLACK", "Slack"),
    ("EMAIL", "Email"),
    ("VPN", "VPN"),
    ("CLAUDE_CODE", "Claude Code"),
    ("GERRIT", "Gerrit"),
    ("GITEA", "Gitea"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0033_gerrit_integration"),
    ]

    operations = [
        migrations.AlterField(
            model_name="repositoryinfo",
            name="type",
            field=models.CharField(choices=_SCM_TYPE_CHOICES, default="GITHUB", max_length=64),
        ),
        migrations.AlterField(
            model_name="orgintegration",
            name="integration_type",
            field=models.CharField(choices=_INTEGRATION_TYPE_CHOICES, max_length=32),
        ),
        migrations.AlterField(
            model_name="projectintegrationoverride",
            name="integration_type",
            field=models.CharField(choices=_INTEGRATION_TYPE_CHOICES, max_length=32),
        ),
        migrations.CreateModel(
            name="ScmGiteaBinding",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "scm",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="gitea_binding",
                        to="aist.repositoryinfo",
                    ),
                ),
                (
                    "org_integration",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        limit_choices_to={"integration_type": "GITEA"},
                        related_name="+",
                        to="aist.orgintegration",
                    ),
                ),
            ],
        ),
    ]
