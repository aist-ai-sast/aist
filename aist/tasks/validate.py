from __future__ import annotations

from celery import shared_task

from aist.models import OrgIntegration


@shared_task(name="aist.tasks.validate.validate_work_item_provider", bind=True)
def validate_work_item_provider(self, provider_id: int, async_user=None) -> dict:
    """
    Run WorkItemProvider credential validation inside a Celery worker.

    Runs in the worker process, which has Docker socket access needed for
    VPN-routed validations (vpn_sidecar_context).  Returns {"valid": bool, "detail": str}.

    _provider_id is embedded in both SUCCESS and FAILURE results so the status
    endpoint can verify the task_id belongs to the expected provider and prevent
    cross-task result disclosure.
    """
    try:
        from aist.models import WorkItemProvider  # noqa: PLC0415

        provider = WorkItemProvider.objects.select_related("vpn_integration", "vpn_integration__vpn_secret").get(
            pk=provider_id,
        )
        from aist.api.work_items import _validate_work_item_provider  # noqa: PLC0415

        valid, detail = _validate_work_item_provider(provider)
    except Exception:
        self.update_state(
            state="FAILURE",
            meta={"_provider_id": provider_id},
        )
        raise
    else:
        return {"valid": valid, "detail": detail, "_provider_id": provider_id}


@shared_task(name="aist.tasks.validate.validate_integration", bind=True)
def validate_integration(self, integration_id: int, async_user=None) -> dict:
    """
    Run integration credential validation inside a Celery worker.

    Runs in the worker process, which has Docker socket access needed for
    VPN-routed validations (vpn_sidecar_context).  Returns {"valid": bool, "detail": str}.

    _integration_id is embedded in both SUCCESS and FAILURE results so the status
    endpoint can verify the task_id belongs to the expected integration and prevent
    cross-task result disclosure.
    """
    try:
        integration = OrgIntegration.objects.select_related("vpn_integration", "vpn_secret").get(pk=integration_id)
        from aist.api.org_integrations import _validate_integration  # noqa: PLC0415

        valid, detail = _validate_integration(integration)
    except Exception:
        # Store integration_id in FAILURE meta so the status endpoint can verify
        # task ownership before returning any error details to the caller.
        self.update_state(
            state="FAILURE",
            meta={"_integration_id": integration_id},
        )
        raise
    else:
        return {"valid": valid, "detail": detail, "_integration_id": integration_id}
