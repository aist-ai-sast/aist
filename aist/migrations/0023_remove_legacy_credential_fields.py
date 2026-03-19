"""
Remove legacy credential fields now superseded by OrgIntegration:

- ScmGitlabBinding.personal_access_token  → was migrated to OrgIntegration(GITLAB)
- ScmGithubBinding.base_api_url           → was migrated to OrgIntegration(GITHUB).config
- AISTLaunchConfigAction.secret_config    → Slack tokens migrated to OrgIntegration(SLACK)
"""
from __future__ import annotations

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0022_migrate_integration_data"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="scmgitlabbinding",
            name="personal_access_token",
        ),
        migrations.RemoveField(
            model_name="scmgithubbinding",
            name="base_api_url",
        ),
        migrations.RemoveField(
            model_name="aistlaunchconfigaction",
            name="secret_config",
        ),
    ]
