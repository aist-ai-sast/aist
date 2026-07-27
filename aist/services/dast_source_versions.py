"""Resolve provider-verified DAST source commits to AIST project versions."""

from __future__ import annotations

from django.db import transaction

from aist.integrations.dast_report import ValidatedDastReport
from aist.models import AISTProject, AISTProjectVersion, DastProjectBinding, VersionType


class DastSourceVersionError(ValueError):

    """A validated DAST report cannot be mapped through its persisted binding."""


def resolve_dast_source_version(
    report: ValidatedDastReport,
    binding: DastProjectBinding,
) -> AISTProjectVersion:
    """Resolve one binding-selected actual commit without repository-name fallbacks."""
    if not isinstance(report, ValidatedDastReport):
        msg = "DAST source resolution requires a validated report."
        raise DastSourceVersionError(msg)
    if binding.pk is None:
        msg = "DAST source resolution requires a persisted binding."
        raise DastSourceVersionError(msg)

    with transaction.atomic():
        persisted_binding = (
            DastProjectBinding.objects
            .select_for_update()
            .select_related("target__integration")
            .get(pk=binding.pk)
        )
        project = AISTProject.objects.select_for_update().get(pk=persisted_binding.project_id)

        if report.target_id != persisted_binding.target.provider_id:
            msg = "DAST report target does not match the source binding."
            raise DastSourceVersionError(msg)
        if project.repository_id is None:
            msg = "The DAST source binding project has no linked repository."
            raise DastSourceVersionError(msg)

        matching_commits = [
            commit
            for repository_key, commit in report.source_commits
            if repository_key == persisted_binding.source_repo_key
        ]
        if not matching_commits:
            msg = "DAST report does not contain the binding source repository."
            raise DastSourceVersionError(msg)
        if len(matching_commits) != 1:
            msg = "DAST report source repository mapping is ambiguous."
            raise DastSourceVersionError(msg)

        version, _created = AISTProjectVersion.objects.get_or_create(
            project=project,
            version=matching_commits[0],
            version_type=VersionType.GIT_HASH,
        )
        return version
