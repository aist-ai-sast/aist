from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aist.models import WorkItemProvider
    from aist.work_items.backends.base import WorkItemBackend

_BACKENDS: dict[str, type] = {}


def register_backend(provider_type: str):
    """
    Class decorator that registers a ``WorkItemBackend`` subclass for a provider type.

    Usage::

        @register_backend("JIRA")
        class JiraBackend(WorkItemBackend):
            ...
    """

    def decorator(cls: type) -> type:
        _BACKENDS[provider_type] = cls
        return cls

    return decorator


def get_backend(provider: WorkItemProvider, proxy_url: str | None = None) -> WorkItemBackend:
    """
    Return an instantiated backend for *provider*.

    Raises ``NotImplementedError`` when no backend is registered for the
    provider type (e.g. GENERIC providers have no sync backend).
    """
    cls = _BACKENDS.get(provider.provider_type)
    if cls is None:
        msg = f"No sync backend registered for provider type '{provider.provider_type}'"
        raise NotImplementedError(msg)
    return cls(provider, proxy_url=proxy_url)


def has_backend(provider_type: str) -> bool:
    """Return True if a sync backend exists for the given provider type."""
    return provider_type in _BACKENDS
