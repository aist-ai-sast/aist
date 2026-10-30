"""Request throttles whose counters every worker shares."""
from __future__ import annotations

import logging

from django.core.cache import cache as process_cache
from django.core.cache import caches
from rest_framework.throttling import ScopedRateThrottle

from aist.utils.cache import QUEUE_CACHE_ALIAS

logger = logging.getLogger(__name__)


class SharedScopedRateThrottle(ScopedRateThrottle):

    """
    ``ScopedRateThrottle`` counting in the store shared by every worker.

    Django's default cache is one process's memory, so a rate configured there
    is really that rate times the worker count. An unreachable shared store
    degrades to counting in this process rather than refusing the request.
    """

    def allow_request(self, request, view):
        self.cache = caches[QUEUE_CACHE_ALIAS]
        try:
            return super().allow_request(request, view)
        except Exception:
            logger.warning("Throttle cache unreachable; counting in this process only", exc_info=True)
            self.cache = process_cache
            return super().allow_request(request, view)


class ProjectScopedRateThrottle(SharedScopedRateThrottle):

    """
    Counts per ``project_id`` in the URL rather than per caller.

    For a write whose cost falls on the project, every member of that project
    draws from one budget; keying on the caller would multiply the bound by the
    number of people holding the role.
    """

    def get_cache_key(self, request, view):
        project_id = view.kwargs.get("project_id")
        if project_id is None:
            return super().get_cache_key(request, view)
        return self.cache_format % {"scope": self.scope, "ident": f"project-{project_id}"}
