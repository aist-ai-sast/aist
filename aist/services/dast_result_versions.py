"""Resolve the one project version a finalized DAST report attaches its findings to."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aist.models import AISTProjectVersion, DastProjectBinding, VersionType
from aist.services.dast_source_versions import resolve_dast_source_version

if TYPE_CHECKING:
    from aist.integrations.dast_report import ValidatedDastReport


def resolve_dast_result_version(
    report: ValidatedDastReport,
    binding: DastProjectBinding,
) -> AISTProjectVersion:
    """
    Return the effective version of one DAST result — always a version, never None.

    Findings reach a project only through a version (they are filtered and displayed by it), so
    a result with no source revision is not version-less: it belongs to the target that produced
    it. A source-bound target resolves to its actual commit; a target with no repository
    requirement resolves to the durable version standing for the target itself.
    """
    source_version = resolve_dast_source_version(report, binding)
    if source_version is not None:
        return source_version
    return ensure_dast_target_version(binding)


def ensure_dast_target_version(binding: DastProjectBinding) -> AISTProjectVersion:
    """Get or create the single version representing one DAST target inside one project."""
    target = binding.target
    version, _created = AISTProjectVersion.objects.get_or_create(
        project_id=binding.project_id,
        version=target.provider_id,
        version_type=VersionType.DAST_TARGET,
        defaults={"description": target.display_name or target.provider_id},
    )
    return version
