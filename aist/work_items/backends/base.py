from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from aist.models import WorkItemLink, WorkItemProvider


@dataclass
class RemoteIssueInfo:

    """Normalised issue information returned by any backend."""

    raw_status: str
    # One of WorkItemStatusCategory values: OPEN / IN_PROGRESS / DONE / CANCELLED / UNKNOWN
    status_category: str
    title: str
    external_url: str = ""
    # Extra backend-specific fields (e.g. assignee, priority) — not stored, just for reference
    extra: dict = field(default_factory=dict)


class WorkItemBackend(ABC):

    """
    Abstract base for all external issue-tracker integrations.

    Concrete subclasses register themselves via ``@register_backend(provider_type)``
    and are picked up automatically when the ``aist.work_items.backends`` package
    is imported.

    Credentials and config are taken from ``self.provider`` at runtime so that
    the same class handles both cloud and self-hosted variants of a tracker.
    """

    def __init__(self, provider: WorkItemProvider, proxy_url: str | None = None) -> None:
        self.provider = provider
        self.proxy_url = proxy_url

    def _proxies(self) -> dict | None:
        """Return a requests-compatible proxies dict, or None when no proxy is set."""
        if not self.proxy_url:
            return None
        return {"http": self.proxy_url, "https": self.proxy_url}

    def _make_proxied_session(self) -> requests.Session:
        """Return a requests.Session pre-configured with the proxy (if any)."""
        session = requests.Session()
        proxies = self._proxies()
        if proxies:
            session.proxies.update(proxies)
        return session

    @abstractmethod
    def validate_credentials(self) -> bool:
        """Return True if the stored credentials are valid, False otherwise."""

    @abstractmethod
    def fetch_issue_status(self, link: WorkItemLink) -> RemoteIssueInfo:
        """
        Fetch current status for the given WorkItemLink from the tracker.

        Backends may use ``link.external_id``, ``link.external_key``,
        or ``link.external_url`` depending on what makes most sense for
        the tracker (e.g. GitHub parses owner/repo from the URL).

        Raises ``WorkItemSyncError`` on transport / auth failures.
        """


class WorkItemSyncError(Exception):

    """Raised when a backend cannot fetch issue status (network, auth, not-found…)."""
