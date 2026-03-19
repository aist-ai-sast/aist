"""
Data migration: move existing credentials into OrgIntegration.

- ScmGitlabBinding.personal_access_token → OrgIntegration(GITLAB)
- ScmGithubBinding.base_api_url → OrgIntegration(GITHUB) config (GitHub Enterprise only)
- AISTLaunchConfigAction.secret_config Slack tokens → OrgIntegration(SLACK)

Raw encrypted values are copied directly between EncryptedCharField columns
(both encrypted with the same Django SECRET_KEY-derived key), so no
intermediate decrypt/re-encrypt step is needed for GitLab PATs.
Slack tokens require decryption because they are stored inside a JSON blob;
we use the actual model class for that.
"""
from __future__ import annotations

from django.db import migrations


def _migrate_gitlab_tokens(apps, schema_editor):
    """
    For every ScmGitlabBinding with a non-empty personal_access_token:
    - find the owning Organization via binding.scm.project.organization
    - get_or_create OrgIntegration(GITLAB, org, name based on base_url)
    - link binding.org_integration to that integration

    The encrypted bytes are copied as-is (same key, same field type).
    """
    ScmGitlabBinding = apps.get_model("aist", "ScmGitlabBinding")
    OrgIntegration = apps.get_model("aist", "OrgIntegration")

    for binding in ScmGitlabBinding.objects.select_related("scm").exclude(personal_access_token=""):
        try:
            project = binding.scm.project
        except Exception:  # noqa: S112
            continue
        if not project or not project.organization_id:
            continue

        base_url = (getattr(binding.scm, "base_url", None) or "https://gitlab.com").rstrip("/")
        integration_name = f"GitLab ({base_url})"

        # Try to find an existing integration for this org+name.
        existing = OrgIntegration.objects.filter(
            organization_id=project.organization_id,
            integration_type="GITLAB",
            name=integration_name,
        ).first()

        if existing:
            integration = existing
        else:
            integration = OrgIntegration(
                organization_id=project.organization_id,
                integration_type="GITLAB",
                name=integration_name,
                config={"base_url": base_url},
                # Copy raw encrypted value; both fields use the same encryption.
                secret=binding.personal_access_token,
                is_active=True,
            )
            integration.save()

        binding.org_integration = integration
        binding.save(update_fields=["org_integration"])


def _migrate_github_base_urls(apps, schema_editor):
    """
    For every ScmGithubBinding with a non-empty base_api_url (GitHub Enterprise):
    - find the owning Organization
    - get_or_create OrgIntegration(GITHUB) with base_api_url in config
    - link binding.org_integration
    """
    ScmGithubBinding = apps.get_model("aist", "ScmGithubBinding")
    OrgIntegration = apps.get_model("aist", "OrgIntegration")

    for binding in ScmGithubBinding.objects.select_related("scm").exclude(base_api_url=""):
        try:
            project = binding.scm.project
        except Exception:  # noqa: S112
            continue
        if not project or not project.organization_id:
            continue

        base_api_url = binding.base_api_url.strip()
        integration_name = f"GitHub Enterprise ({base_api_url})"

        existing = OrgIntegration.objects.filter(
            organization_id=project.organization_id,
            integration_type="GITHUB",
            name=integration_name,
        ).first()

        if existing:
            integration = existing
        else:
            integration = OrgIntegration(
                organization_id=project.organization_id,
                integration_type="GITHUB",
                name=integration_name,
                config={"base_api_url": base_api_url},
                secret="",
                is_active=True,
            )
            integration.save()

        binding.org_integration = integration
        binding.save(update_fields=["org_integration"])


def _migrate_slack_tokens(apps, schema_editor):
    """
    For every AISTLaunchConfigAction with a Slack token in secret_config:
    - decrypt using historical model (secret_config EncryptedCharField still present at this point)
    - find the owning Organization
    - get_or_create OrgIntegration(SLACK) for the org
    - link project via ProjectIntegrationOverride is NOT created here
      (channel config stays in action.config, token moves to org level)
    """
    import json  # noqa: PLC0415

    # Use historical model so secret_config is still available in the schema
    # (it is removed in the next migration 0023). EncryptedCharField is preserved
    # in the historical state, so decryption via from_db_value still works.
    AISTLaunchConfigAction = apps.get_model("aist", "AISTLaunchConfigAction")
    OrgIntegration = apps.get_model("aist", "OrgIntegration")

    seen_org_tokens: dict[tuple[int, str], int] = {}  # (org_id, token) -> integration_id

    for action in AISTLaunchConfigAction.objects.select_related(
        "launch_config__project__organization",
    ).filter(action_type="PUSH_TO_SLACK").exclude(secret_config=""):
        try:
            raw = action.secret_config  # decrypted by EncryptedCharField
            secret_dict = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:  # noqa: S112
            continue

        slack_token = (secret_dict.get("slack_token") or "").strip()
        if not slack_token:
            continue

        project = action.launch_config.project
        if not project or not project.organization_id:
            continue

        org_id = project.organization_id
        cache_key = (org_id, slack_token)

        if cache_key in seen_org_tokens:
            continue  # already created for this org+token

        existing = OrgIntegration.objects.filter(
            organization_id=org_id,
            integration_type="SLACK",
        ).first()

        if existing:
            # Fill secret only if the existing integration has none — don't
            # overwrite a token the user already configured deliberately.
            if not (existing.secret or "").strip():
                existing.secret = slack_token
                existing.save(update_fields=["secret"])
            seen_org_tokens[cache_key] = existing.pk
            continue

        integration = OrgIntegration(
            organization_id=org_id,
            integration_type="SLACK",
            name="Slack",
            config={},
            secret=slack_token,
            is_active=True,
        )
        integration.save()
        seen_org_tokens[cache_key] = integration.pk


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0021_org_integration"),
    ]

    operations = [
        migrations.RunPython(_migrate_gitlab_tokens, migrations.RunPython.noop),
        migrations.RunPython(_migrate_github_base_urls, migrations.RunPython.noop),
        migrations.RunPython(_migrate_slack_tokens, migrations.RunPython.noop),
    ]
