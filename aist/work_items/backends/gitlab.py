from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import gitlab
import gitlab.exceptions

from aist.models import WorkItemStatusCategory
from aist.work_items.backends.base import RemoteIssueInfo, WorkItemBackend, WorkItemSyncError
from aist.work_items.backends.registry import register_backend

if TYPE_CHECKING:
    from aist.models import WorkItemLink

logger = logging.getLogger("aist.work_items")

# GitLab issue state → our category
_STATE_TO_CATEGORY: dict[str, str] = {
    "opened": WorkItemStatusCategory.OPEN,
    "closed": WorkItemStatusCategory.DONE,
}


@register_backend("GITLAB")
class GitlabIssuesBackend(WorkItemBackend):

    """
    Sync backend for GitLab Issues (cloud and self-managed).

    Uses the ``python-gitlab`` library (already in requirements).

    Required provider fields:
    - ``api_token``: GitLab personal access token or project access token
    - ``provider_config["project_id"]``: GitLab numeric project ID or
      "namespace/project" path

    Optional:
    - ``base_url``: override for self-managed GitLab, e.g. "https://gitlab.company.com"
      (leave blank for gitlab.com)
    """

    def _build_client(self):
        url = (self.provider.base_url or "").rstrip("/") or "https://gitlab.com"
        token = (self.provider.api_token or "").strip()
        if not token:
            msg = "WorkItemProvider.api_token must be set for GITLAB providers"
            raise WorkItemSyncError(msg)

        return gitlab.Gitlab(url, private_token=token, session=self._make_proxied_session())

    def _get_project(self, gl):
        cfg = self.provider.provider_config or {}
        project_id = cfg.get("project_id")
        if not project_id:
            msg = "provider_config must contain 'project_id' for GITLAB providers"
            raise WorkItemSyncError(msg)
        try:
            return gl.projects.get(project_id)
        except gitlab.exceptions.GitlabGetError as exc:
            msg = f"GitLab project '{project_id}' not found: {exc}"
            raise WorkItemSyncError(msg) from exc

    def validate_credentials(self) -> bool:
        try:
            gl = self._build_client()
            gl.auth()
        except (WorkItemSyncError, gitlab.exceptions.GitlabAuthenticationError, Exception):
            logger.debug("GitLab credential validation failed for provider %s", self.provider.pk, exc_info=True)
            return False
        else:
            return True

    def fetch_issue_status(self, link: WorkItemLink) -> RemoteIssueInfo:
        issue_id = (link.external_id or link.external_key or "").strip()
        try:
            gl = self._build_client()
            project = self._get_project(gl)
            # issue IID is the project-scoped number
            issue_iid = int(issue_id.lstrip("#"))
            issue = project.issues.get(issue_iid)
        except (ValueError, gitlab.exceptions.GitlabGetError) as exc:
            msg = f"GitLab issue '{issue_id}' not found: {exc}"
            raise WorkItemSyncError(msg) from exc

        state: str = issue.state
        # GitLab marks closed issues; check `closed_as` (available in newer versions)
        closed_as = getattr(issue, "closed_as", None)
        if state == "closed" and closed_as == "unresolved":
            status_category = WorkItemStatusCategory.CANCELLED
        else:
            status_category = _STATE_TO_CATEGORY.get(state, WorkItemStatusCategory.UNKNOWN)

        return RemoteIssueInfo(
            raw_status=state,
            status_category=status_category,
            title=issue.title or "",
            external_url=issue.web_url or "",
        )
