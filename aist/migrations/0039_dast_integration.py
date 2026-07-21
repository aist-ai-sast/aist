# Generated for: dast/runtime/reports/_plans/aist-dast-integration-plan.md (Step 8)

from django.db import migrations, models

_INTEGRATION_TYPE_CHOICES = [
    ("GITLAB", "GitLab"),
    ("GITHUB", "GitHub"),
    ("SLACK", "Slack"),
    ("EMAIL", "Email"),
    ("VPN", "VPN"),
    ("CLAUDE_CODE", "Claude Code"),
    ("GERRIT", "Gerrit"),
    ("GITEA", "Gitea"),
    ("DAST", "DAST"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0038_org_member_access_scope"),
    ]

    operations = [
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
    ]
