from __future__ import annotations

from dataclasses import dataclass

from aist.models import AISTProjectVersion, VersionType


@dataclass(frozen=True)
class ProjectVersionGitRefs:
    branch: str | None
    commit: str | None


def resolve_project_version_git_refs(project_version: AISTProjectVersion | None) -> ProjectVersionGitRefs:
    if not project_version:
        return ProjectVersionGitRefs(branch=None, commit=None)

    if project_version.version_type == VersionType.GIT_HASH:
        commit = str(project_version.version or "").strip() or None
        branch = None
        if project_version.resolved_from_branch_id:
            branch = str(project_version.resolved_from_branch.version or "").strip() or None
        return ProjectVersionGitRefs(branch=branch, commit=commit)

    if project_version.version_type == VersionType.GIT_BRANCH:
        branch = str(project_version.version or "").strip() or None
        return ProjectVersionGitRefs(branch=branch, commit=None)

    return ProjectVersionGitRefs(branch=None, commit=None)
