from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import requests

from aist.models import WorkItemStatusCategory
from aist.work_items.backends.base import RemoteIssueInfo, WorkItemBackend, WorkItemSyncError
from aist.work_items.backends.registry import register_backend

if TYPE_CHECKING:
    from aist.models import WorkItemLink

logger = logging.getLogger("aist.work_items")

_GITHUB_API = "https://api.github.com"
_TIMEOUT = 10  # seconds

# GitHub issue state → our category.
# ``state_reason`` refines "closed" into DONE vs CANCELLED ("not_planned").
_STATE_TO_CATEGORY: dict[str, str] = {
    "open": WorkItemStatusCategory.OPEN,
    "closed": WorkItemStatusCategory.DONE,
}


@register_backend("GITHUB")
class GithubIssuesBackend(WorkItemBackend):

    """
    Sync backend for GitHub Issues (cloud and GHES).

    Auth priority:
    1. ``api_token`` on the provider (PAT or fine-grained token).
    2. GitHub App installation already linked to the organisation — zero extra
       config needed if the org imported a project via the GitHub App.

    ``base_url`` is optional. When blank, it is auto-discovered from any existing
    ``ScmGithubBinding`` in the organisation (useful for GHES).

    ``provider_config`` needs no ``repo_owner`` / ``repo_name`` — they are
    parsed from each link's ``external_url`` at sync time.
    """

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _installation_binding(self):
        """Return the first ScmGithubBinding for any project in this org, or None."""
        from aist.models import ScmGithubBinding  # noqa: PLC0415

        return (
            ScmGithubBinding.objects.filter(
                scm__project__organization=self.provider.organization,
            )
            .select_related("scm")
            .first()
        )

    def _api_base(self) -> str:
        base = (self.provider.base_url or "").rstrip("/")
        if base:
            return base
        binding = self._installation_binding()
        if binding and binding.org_integration:
            base_api_url = (binding.org_integration.config or {}).get("base_api_url", "")
            if base_api_url:
                return base_api_url.rstrip("/")
        return _GITHUB_API

    def _headers(self) -> dict[str, str]:
        base = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        token = (self.provider.api_token or "").strip()
        if token:
            base["Authorization"] = f"Bearer {token}"
            return base
        # Fall back to existing GitHub App installation in the org
        binding = self._installation_binding()
        if binding:
            install_headers = binding.get_auth_headers()
            if install_headers:
                base.update(install_headers)
        return base

    # ------------------------------------------------------------------
    # Repo / issue resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_repo_from_url(url: str) -> tuple[str, str] | None:
        """
        Extract (owner, repo) from a GitHub issue URL.

        Handles both cloud and GHES URLs:
          https://github.com/owner/repo/issues/42
          https://github.company.com/owner/repo/issues/42
        """
        try:
            parts = urlparse(url).path.strip("/").split("/")
            # parts: ['owner', 'repo', 'issues', '42']
            if len(parts) >= 4 and parts[2] == "issues":
                return parts[0], parts[1]
        except Exception:  # noqa: S110
            pass
        return None

    @staticmethod
    def _issue_number_from_link(link: WorkItemLink) -> str:
        """Return the issue number as a plain string (strip leading #)."""
        return (link.external_id or link.external_key or "").lstrip("#")

    # ------------------------------------------------------------------
    # Backend interface
    # ------------------------------------------------------------------

    def validate_credentials(self) -> bool:
        try:
            resp = requests.get(
                f"{self._api_base()}/user",
                headers=self._headers(),
                timeout=_TIMEOUT,
            )
        except requests.RequestException:
            logger.debug("GitHub credential validation failed for provider %s", self.provider.pk, exc_info=True)
            return False
        else:
            return resp.status_code == 200

    def fetch_issue_status(self, link: WorkItemLink) -> RemoteIssueInfo:
        # Resolve owner/repo: parse from external_url, fall back to provider_config
        repo_parts = self._parse_repo_from_url(link.external_url or "")
        if not repo_parts:
            cfg = self.provider.provider_config or {}
            owner = cfg.get("repo_owner", "").strip()
            repo = cfg.get("repo_name", "").strip()
            if not owner or not repo:
                msg = (
                    "Cannot determine repository for this link. "
                    "Provide a full GitHub issue URL or set provider_config['repo_owner'/'repo_name']."
                )
                raise WorkItemSyncError(msg)
            repo_parts = (owner, repo)

        owner, repo = repo_parts
        issue_number = self._issue_number_from_link(link)
        if not issue_number:
            msg = "WorkItemLink has no external_id or external_key to identify the issue number"
            raise WorkItemSyncError(msg)

        url = f"{self._api_base()}/repos/{owner}/{repo}/issues/{issue_number}"

        try:
            resp = requests.get(url, headers=self._headers(), timeout=_TIMEOUT)
        except requests.RequestException as exc:
            msg = f"GitHub request failed: {exc}"
            raise WorkItemSyncError(msg) from exc

        if resp.status_code == 404:
            msg = f"GitHub issue #{issue_number} not found in {owner}/{repo}"
            raise WorkItemSyncError(msg)
        if resp.status_code == 401:
            msg = "GitHub API returned 401 Unauthorized - check api_token or GitHub App installation"
            raise WorkItemSyncError(msg)
        if not resp.ok:
            msg = f"GitHub API error {resp.status_code}: {resp.text[:200]}"
            raise WorkItemSyncError(msg)

        data = resp.json()
        state: str = data.get("state", "open")
        state_reason: str | None = data.get("state_reason")

        if state == "closed" and state_reason == "not_planned":
            status_category = WorkItemStatusCategory.CANCELLED
        else:
            status_category = _STATE_TO_CATEGORY.get(state, WorkItemStatusCategory.UNKNOWN)

        raw_status = state if not state_reason else f"{state} ({state_reason})"

        return RemoteIssueInfo(
            raw_status=raw_status,
            status_category=status_category,
            title=data.get("title", ""),
            external_url=data.get("html_url", ""),
        )
