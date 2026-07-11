"""
Celery entrypoints for the warm per-VPN egress gateway.

Thin wrappers only — all policy lives in ``aist/integrations/egress.py``.  These
run in the celeryworker (Docker socket access); the web process never manages
containers, it only enqueues :func:`prewarm_egress`.
"""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="aist.tasks.egress.prewarm_egress")
def prewarm_egress(project_version_id: int, *, async_user=None) -> str | None:
    """
    Ensure a warm egress tunnel exists for a project version's VPN, if any.

    Idempotent and cheap when already warm.  No-op (returns ``None``) when the
    version needs no VPN.  Never raises to the caller — prewarm is best-effort;
    the blob request degrades to a ``202 warming`` retry if the tunnel is not up
    yet.

    ``async_user`` is injected by DefectDojo's ``DojoAsyncTask.apply_async`` into
    every task call and is accepted (unused) here to satisfy that contract.
    """
    del async_user  # unused; accepted only for the DojoAsyncTask contract
    from aist.integrations import egress  # noqa: PLC0415
    from aist.models import AISTProjectVersion  # noqa: PLC0415

    pv = (
        AISTProjectVersion.objects.select_related(
            "project__repository",
        )
        .filter(pk=project_version_id)
        .first()
    )
    if pv is None:
        logger.warning("prewarm_egress: project_version=%s not found", project_version_id)
        return None

    vpn_integration = egress.vpn_integration_for_project_version(pv)
    if vpn_integration is None:
        return None

    try:
        return egress.ensure_warm(vpn_integration)
    except Exception:
        logger.warning("prewarm_egress: failed to warm vpn=%s", vpn_integration.id, exc_info=True)
        return None


@shared_task(name="aist.tasks.egress.reap_egress")
def reap_egress(*, async_user=None) -> int:
    """Stop idle / over-cap warm-egress containers.  Returns count removed."""
    del async_user  # unused; accepted only for the DojoAsyncTask contract
    from aist.integrations import egress  # noqa: PLC0415

    return egress.reap_idle()
