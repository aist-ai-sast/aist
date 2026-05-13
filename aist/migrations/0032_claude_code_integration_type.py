# Generated for: docs/plans/2026-05-12-claude-as-org-integration.md (Task 1)

from django.db import migrations, models

_INTEGRATION_TYPE_CHOICES = [
    ("GITLAB", "GitLab"),
    ("GITHUB", "GitHub"),
    ("SLACK", "Slack"),
    ("EMAIL", "Email"),
    ("VPN", "VPN"),
    ("CLAUDE_CODE", "Claude Code"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0031_aistairesponse_source"),
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
        migrations.AddConstraint(
            model_name="orgintegration",
            constraint=models.UniqueConstraint(
                condition=models.Q(("integration_type", "CLAUDE_CODE"), ("is_active", True)),
                fields=("organization",),
                name="one_active_claude_integration_per_org",
            ),
        ),
    ]
