"""
Construct a ``BridgeClient`` instance from Django settings.

Keeps Django settings access in ONE place: AIST callers
(``aist/tasks/ai.py``, ``aist/tasks/pipeline.py``) call
``build_bridge_client_from_settings()`` and pass the result to
``pipeline.bridge_client``-using code, which itself is Django-agnostic.
"""
from __future__ import annotations

from django.conf import settings

from aist.utils.pipeline_imports import _import_sast_pipeline_package

_import_sast_pipeline_package()

from pipeline.bridge_client import BridgeClient  # noqa: E402  # imports require pipeline package


def build_bridge_client_from_settings() -> BridgeClient:
    """
    Return a ``BridgeClient`` bound to the bridge UDS configured in settings.

    - ``socket_path`` from ``settings.AIST_LOCAL_TRIAGE_BRIDGE_SOCKET``
      (default ``/run/claude-bridge/bridge.sock`` per the docker-compose layout).
    - ``sync_timeout_seconds`` from ``settings.AIST_LOCAL_TRIAGE_TIMEOUT + 60``
      so the HTTP read timeout is always larger than the bridge's own
      internal claude-CLI timeout.
    - ``async_timeout_seconds`` short (10s) — ``/analyze`` is fire-and-forget
      and only needs to enqueue.
    """
    return BridgeClient(
        socket_path=settings.AIST_LOCAL_TRIAGE_BRIDGE_SOCKET,
        sync_timeout_seconds=int(settings.AIST_LOCAL_TRIAGE_TIMEOUT) + 60,
        async_timeout_seconds=10,
    )
