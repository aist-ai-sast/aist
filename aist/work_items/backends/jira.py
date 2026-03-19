from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from jira import JIRA, JIRAError

from aist.models import WorkItemStatusCategory
from aist.work_items.backends.base import RemoteIssueInfo, WorkItemBackend, WorkItemSyncError
from aist.work_items.backends.registry import register_backend

if TYPE_CHECKING:
    from aist.models import WorkItemLink

logger = logging.getLogger("aist.work_items")

# Jira status-category key → our normalised category
# https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-statuses/
_JIRA_CATEGORY_MAP: dict[str, str] = {
    "new": WorkItemStatusCategory.OPEN,
    "indeterminate": WorkItemStatusCategory.IN_PROGRESS,
    "done": WorkItemStatusCategory.DONE,
}


@register_backend("JIRA")
class JiraBackend(WorkItemBackend):

    """
    Sync backend for Jira Cloud and Jira Data Center.

    Uses the ``jira`` Python library (jira==3.10.5, already in vendor requirements).

    Required provider fields:
    - ``base_url``: Jira instance root, e.g. "https://company.atlassian.net"
    - ``api_token``: API token (Cloud ATATT…) or PAT (Data Center)
    - ``provider_config["jira_email"]``: account email — required for Jira Cloud;
      omit for Jira Data Center/Server (uses Bearer PAT auth instead)
    """

    def _build_client(self) -> JIRA:
        """
        Return an authenticated jira.JIRA client.

        Jira Cloud API tokens (ATATT…) require Basic auth with email+token.
        Jira Data Center PATs use Bearer token auth (no email needed).
        """
        server = (self.provider.base_url or "").rstrip("/")
        if not server:
            msg = "WorkItemProvider.base_url must be set for JIRA providers"
            raise WorkItemSyncError(msg)

        token = (self.provider.api_token or "").strip()
        if not token:
            msg = "WorkItemProvider.api_token must be set for JIRA providers"
            raise WorkItemSyncError(msg)

        email = (self.provider.provider_config or {}).get("jira_email", "").strip()

        try:
            if email:
                # Jira Cloud: API token requires Basic auth (email + token)
                return JIRA(server=server, basic_auth=(email, token))
            # Jira Data Center/Server: PAT uses Bearer auth
            return JIRA(server=server, token_auth=token)
        except JIRAError as exc:
            raise WorkItemSyncError(str(exc)) from exc

    def validate_credentials(self) -> bool:
        try:
            client = self._build_client()
            client.myself()
        except (WorkItemSyncError, Exception):
            logger.debug("Jira credential validation failed for provider %s", self.provider.pk, exc_info=True)
            return False
        else:
            return True

    def fetch_issue_status(self, link: WorkItemLink) -> RemoteIssueInfo:
        issue_key = (link.external_id or link.external_key or "").strip()
        try:
            client = self._build_client()
            issue = client.issue(issue_key, fields="summary,status")
        except JIRAError as exc:
            msg = f"Jira API error for '{issue_key}': {exc}"
            raise WorkItemSyncError(msg) from exc

        status_name: str = issue.fields.status.name
        category_key: str = issue.fields.status.statusCategory.key
        status_category = _JIRA_CATEGORY_MAP.get(category_key, WorkItemStatusCategory.UNKNOWN)

        server = (self.provider.base_url or "").rstrip("/")
        issue_url = f"{server}/browse/{issue.key}" if server else ""

        return RemoteIssueInfo(
            raw_status=status_name,
            status_category=status_category,
            title=issue.fields.summary or "",
            external_url=issue_url,
        )
