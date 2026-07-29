from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from django.db import transaction

from aist.models import (
    AISTPipeline,
    AISTProject,
    AISTProjectLaunchConfig,
    DastIntegrationState,
    DastIntegrationValidationState,
    DastOnboardingBundleUse,
    DastProjectBinding,
    DastTarget,
    LaunchSchedule,
    OrgIntegration,
    OrgIntegrationType,
    PipelineExecutionLease,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)
from aist.services.pipeline_lifecycle import TERMINAL_PIPELINE_STATUSES


class DastIntegrationLifecycleCode(StrEnum):
    INTEGRATION_MUST_BE_DISABLED = "INTEGRATION_MUST_BE_DISABLED"
    EXECUTION_ACTIVE = "EXECUTION_ACTIVE"
    INTEGRATION_TYPE_INVALID = "INTEGRATION_TYPE_INVALID"


class DastIntegrationLifecycleError(RuntimeError):
    def __init__(self, code: DastIntegrationLifecycleCode, *, dependency_count: int = 0):
        self.code = code
        self.dependency_count = max(0, int(dependency_count))
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class DastDisableResult:
    integration_id: int
    changed: bool
    disabled_schedule_count: int


@dataclass(frozen=True, slots=True)
class DastDeleteResult:
    integration_id: int
    deleted_requests: int
    deleted_configs: int
    deleted_bindings: int
    deleted_targets: int


def disable_dast_integration(integration_id: int) -> DastDisableResult:
    with transaction.atomic():
        integration = OrgIntegration.objects.select_for_update().get(pk=integration_id)
        if integration.integration_type != OrgIntegrationType.DAST:
            raise DastIntegrationLifecycleError(DastIntegrationLifecycleCode.INTEGRATION_TYPE_INVALID)
        state = DastIntegrationState.objects.select_for_update().get(integration=integration)
        changed = integration.is_active
        if not changed:
            return DastDisableResult(
                integration_id=integration.pk,
                changed=False,
                disabled_schedule_count=0,
            )
        integration.is_active = False
        integration.save(update_fields=["is_active", "updated"])

        state.validation_generation += 1
        state.validation_task_id = ""
        state.validation_claimed_at = None
        state.validation_state = DastIntegrationValidationState.PENDING_VALIDATION
        state.validation_error_code = "INTEGRATION_DISABLED"
        state.sync_generation += 1
        state.sync_task_id = ""
        state.sync_claimed_at = None
        state.sync_error_code = "INTEGRATION_DISABLED"
        state.save(update_fields=[
            "validation_generation",
            "validation_task_id",
            "validation_claimed_at",
            "validation_state",
            "validation_error_code",
            "sync_generation",
            "sync_task_id",
            "sync_claimed_at",
            "sync_error_code",
            "updated",
        ])
        disabled_schedule_count = LaunchSchedule.objects.filter(
            launch_config__execution_type="DAST",
            launch_config__dast_binding__target__integration=integration,
            enabled=True,
        ).update(enabled=False, next_run_at=None)
        return DastDisableResult(
            integration_id=integration.pk,
            changed=changed,
            disabled_schedule_count=disabled_schedule_count,
        )


def delete_dast_integration(integration_id: int) -> DastDeleteResult:
    with transaction.atomic():
        integration = OrgIntegration.objects.select_for_update().get(pk=integration_id)
        if integration.integration_type != OrgIntegrationType.DAST:
            raise DastIntegrationLifecycleError(DastIntegrationLifecycleCode.INTEGRATION_TYPE_INVALID)
        if integration.is_active:
            raise DastIntegrationLifecycleError(
                DastIntegrationLifecycleCode.INTEGRATION_MUST_BE_DISABLED,
            )
        DastIntegrationState.objects.select_for_update().filter(integration=integration).first()
        target_ids = list(
            DastTarget.objects.select_for_update().filter(integration=integration).order_by("pk")
            .values_list("pk", flat=True),
        )
        binding_ids = list(
            DastProjectBinding.objects.select_for_update().filter(target_id__in=target_ids).order_by("pk")
            .values_list("pk", flat=True),
        )
        project_ids = list(
            DastProjectBinding.objects.filter(pk__in=binding_ids)
            .order_by("project_id")
            .values_list("project_id", flat=True)
            .distinct(),
        )
        list(AISTProject.objects.select_for_update().filter(pk__in=project_ids).order_by("pk").values_list("pk"))

        requests = PipelineLaunchRequest.objects.select_for_update().filter(
            execution_type="DAST",
            dast_binding_id__in=binding_ids,
        )
        active_requests = requests.exclude(
            state__in={
                PipelineLaunchRequestState.SUPERSEDED,
                PipelineLaunchRequestState.FAILED,
                PipelineLaunchRequestState.EXPIRED,
                PipelineLaunchRequestState.CANCELLED,
            },
        ).exclude(
            state=PipelineLaunchRequestState.DISPATCHED,
            pipeline__status__in=TERMINAL_PIPELINE_STATUSES,
        )
        active_count = active_requests.count()
        active_count += AISTPipeline.objects.filter(
            launch_request__in=requests,
        ).exclude(status__in=TERMINAL_PIPELINE_STATUSES).count()
        active_count += PipelineExecutionLease.objects.filter(
            request__in=requests,
            released_at__isnull=True,
        ).count()
        if active_count:
            raise DastIntegrationLifecycleError(
                DastIntegrationLifecycleCode.EXECUTION_ACTIVE,
                dependency_count=active_count,
            )

        request_count = requests.count()
        PipelineExecutionLease.objects.filter(request__in=requests).delete()
        requests.delete()
        configs = AISTProjectLaunchConfig.objects.filter(
            execution_type="DAST",
            dast_binding_id__in=binding_ids,
        )
        config_count = configs.count()
        configs.delete()
        binding_count = len(binding_ids)
        DastProjectBinding.objects.filter(pk__in=binding_ids).delete()
        target_count = len(target_ids)
        DastTarget.objects.filter(pk__in=target_ids).delete()
        DastOnboardingBundleUse.objects.filter(org_integration=integration).update(org_integration=None)
        DastIntegrationState.objects.filter(integration=integration).delete()
        integration.delete()
        return DastDeleteResult(
            integration_id=integration_id,
            deleted_requests=request_count,
            deleted_configs=config_count,
            deleted_bindings=binding_count,
            deleted_targets=target_count,
        )
