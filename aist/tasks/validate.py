from __future__ import annotations

from celery import shared_task

from aist.models import OrgIntegration


@shared_task(name="aist.tasks.validate.validate_integration")
def validate_integration(integration_id: int, async_user=None) -> dict:
    """
    Run integration credential validation inside a Celery worker.

    Runs in the worker process, which has Docker socket access needed for
    VPN-routed validations (vpn_sidecar_context).  Returns {"valid": bool, "detail": str}.
    """
    integration = (
        OrgIntegration.objects
        .select_related("vpn_integration", "vpn_secret")
        .get(pk=integration_id)
    )
    from aist.api.org_integrations import _validate_integration  # noqa: PLC0415
    valid, detail = _validate_integration(integration)
    # _integration_id is embedded so the status endpoint can verify the task_id
    # belongs to the expected integration (prevents cross-task result fishing).
    return {"valid": valid, "detail": detail, "_integration_id": integration_id}
