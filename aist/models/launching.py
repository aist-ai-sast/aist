"""Launch schedules, launch requests and leases, and saved launch configurations."""

from __future__ import annotations

import uuid
from datetime import datetime as dt

from croniter import croniter
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from aist.execution.launch_request import (
    LaunchRequestSnapshotError,
    LaunchRequestSnapshots,
    validated_secret_free_json,
)
from aist.models.dast import DastProjectBinding, launch_violation_errors
from aist.models.pipelines import AISTPipeline, AISTStatus, PipelineExecutionType
from aist.models.projects import AISTProject, AISTProjectVersion


def _ensure_aware(value: dt) -> dt:
    if timezone.is_naive(value):
        return timezone.make_aware(value, timezone.get_default_timezone())
    return value


def _dast_launch_errors(launch: PipelineLaunchRequest | AISTProjectLaunchConfig) -> dict[str, str]:
    """Field errors of a DAST launch's binding and trigger version."""
    return launch_violation_errors(
        launch.dast_binding.launch_violations(
            project_id=launch.project_id,
            trigger_version=launch.trigger_project_version,
        ),
        binding_field="dast_binding",
        trigger_field="trigger_project_version",
    )


class LaunchSchedule(models.Model):
    cron_expression = models.CharField(
        max_length=100,
        help_text="Cron expression in standard 5-field format (e.g. '0 15 * * 1' for Mondays at 15:00).",
    )
    enabled = models.BooleanField(default=True)

    max_concurrent_runs = models.PositiveIntegerField(
        default=1,
        help_text="Maximum number of concurrent pipeline runs this schedule's own resource slot allows.",
    )

    launch_config = models.OneToOneField(
        "AISTProjectLaunchConfig",
        on_delete=models.CASCADE,
        related_name="launch_schedule",
        help_text="Anchor launch config. Project is derived from launch_config.project.",
    )

    last_run_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the last time this schedule launched pipelines.",
    )
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True, default="")
    last_error_detail = models.CharField(max_length=512, blank=True, default="")

    class Meta:
        verbose_name = "Launch Schedule"
        verbose_name_plural = "Launch Schedules"
        indexes = [
            models.Index(fields=["enabled", "next_run_at"], name="aist_schedule_due_idx"),
        ]

    def __str__(self) -> str:
        return (f"LaunchSchedule(project={self.launch_config.project_id}, "
                f"launch_config={self.launch_config_id}, cron={self.cron_expression})")

    def get_next_run_time(self, *, now=None):
        """
        Return the most recent scheduled tick time that is <= now (i.e. "due time").

        This avoids missing a tick when the scheduler task runs slightly late,
        e.g. tick at 12:55 but Celery beat triggers at 12:56.
        """
        now = now or timezone.now()
        if timezone.is_naive(now):
            now = timezone.make_aware(now, timezone.get_default_timezone())

        # The last scheduled time at or before "now"
        itr = croniter(self.cron_expression, now)
        due_time = itr.get_prev(dt)

        if timezone.is_naive(due_time):
            due_time = timezone.make_aware(due_time, timezone.get_default_timezone())

        return due_time

    def get_next_scheduled_time(self, *, now=None):
        """
        Return the next scheduled tick strictly after now.

        This is a UI/helper method and must not be used to decide "due".
        Scheduler semantics should keep using get_next_run_time() (prev <= now).
        """
        now = now or timezone.now()
        now = _ensure_aware(now)

        itr = croniter(self.cron_expression, now)
        nxt = itr.get_next(dt)
        return _ensure_aware(nxt)

    def preview_next_runs(self, *, count: int = 5, now=None) -> list[dt]:
        """
        Return next N scheduled ticks after now (strictly in the future).
        Used by UI preview; backend-only logic.
        """
        now = now or timezone.now()
        now = _ensure_aware(now)

        try:
            count = int(count or 0)
        except (TypeError, ValueError):
            count = 5

        if count < 1:
            return []
        count = min(count, 20)

        itr = croniter(self.cron_expression, now)
        out: list[dt] = []
        for _ in range(count):
            nxt = itr.get_next(dt)
            out.append(_ensure_aware(nxt))
        return out


class PipelineLaunchOrigin(models.TextChoices):
    MANUAL = "MANUAL", "Manual"
    SCHEDULE = "SCHEDULE", "Schedule"
    SCM_WEBHOOK = "SCM_WEBHOOK", "SCM webhook"
    RECONCILER = "RECONCILER", "Reconciler"


class PipelineLaunchAuthorityKind(models.TextChoices):
    USER = "USER", "User session"
    PAT = "PAT", "AIST personal access token"
    SCHEDULE = "SCHEDULE", "Stored schedule authority"
    SCM_WEBHOOK = "SCM_WEBHOOK", "Verified SCM webhook"
    RECONCILER = "RECONCILER", "Existing request reconciliation"


class PipelineLaunchRequestState(models.TextChoices):
    PENDING = "PENDING", "Pending"
    CLAIMED = "CLAIMED", "Claimed"
    PLANNED = "PLANNED", "Planned"
    PUBLISHED = "PUBLISHED", "Published"
    DISPATCHED = "DISPATCHED", "Dispatched"
    SUPERSEDED = "SUPERSEDED", "Superseded"
    FAILED = "FAILED", "Failed"
    EXPIRED = "EXPIRED", "Expired"
    CANCELLED = "CANCELLED", "Cancelled"


class PipelineLaunchRequest(models.Model):

    """
    Durable launch request and broker outbox shared by every execution type.

    Dynamic parameter and capability values are frozen at enqueue time. Credentials are
    referenced only by their public database record and never copied into JSON snapshots.
    """

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    updated = models.DateTimeField(auto_now=True)
    origin = models.CharField(
        max_length=24,
        choices=PipelineLaunchOrigin.choices,
        default=PipelineLaunchOrigin.SCHEDULE,
    )
    execution_type = models.CharField(
        max_length=24,
        choices=PipelineExecutionType.choices,
        default=PipelineExecutionType.SAST,
        db_index=True,
    )
    project = models.ForeignKey(AISTProject, on_delete=models.CASCADE)
    dast_binding = models.ForeignKey(
        "DastProjectBinding",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="launch_requests",
    )
    trigger_project_version = models.ForeignKey(
        AISTProjectVersion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="launch_requests",
    )
    schedule = models.ForeignKey(
        LaunchSchedule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    launch_config = models.ForeignKey(
        "AISTProjectLaunchConfig",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="launch_requests",
        help_text="Launch config used to build pipeline_args snapshot.",
    )
    dispatched_at = models.DateTimeField(null=True, blank=True)
    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pipeline_launch_requests",
    )
    api_token = models.ForeignKey(
        "AISTApiToken",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pipeline_launch_requests",
        help_text="Public PAT record used for authority revalidation; never stores the token secret.",
    )
    authority_kind = models.CharField(
        max_length=24,
        choices=PipelineLaunchAuthorityKind.choices,
        default=PipelineLaunchAuthorityKind.SCHEDULE,
    )
    params_snapshot = models.JSONField(default=dict, blank=True)
    capability_snapshot = models.JSONField(default=dict, blank=True)
    initial_launch_data_snapshot = models.JSONField(
        default=dict,
        blank=True,
        editable=False,
        help_text="Secret-free pipeline launch metadata frozen by the authorized producer.",
    )
    state = models.CharField(
        max_length=24,
        choices=PipelineLaunchRequestState.choices,
        default=PipelineLaunchRequestState.PENDING,
        db_index=True,
    )
    coalesce_key = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    superseded_by = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="superseded_requests",
    )
    priority = models.SmallIntegerField(default=0)
    not_before = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    capacity_retry_count = models.PositiveIntegerField(default=0)
    claim_owner = models.CharField(max_length=255, null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    task_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    task_name = models.CharField(
        max_length=128,
        blank=True,
        default="",
        editable=False,
        help_text="Trusted Celery task selected by the launch adapter during planning.",
    )
    task_args_snapshot = models.JSONField(
        default=list,
        blank=True,
        editable=False,
        help_text="Secret-free JSON task arguments frozen before broker publication.",
    )
    client_request_key_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
        editable=False,
        help_text="Server-namespaced digest of an optional producer idempotency key.",
    )
    failure_code = models.CharField(max_length=64, blank=True, default="")
    failure_detail = models.TextField(blank=True, default="")
    pipeline = models.OneToOneField(
        "AISTPipeline",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="launch_request",
    )

    class Meta:
        ordering = ["created"]
        indexes = [
            models.Index(
                fields=["state", "not_before", "priority"],
                name="aist_launch_req_dispatch_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state=PipelineLaunchRequestState.SUPERSEDED,
                        superseded_by__isnull=False,
                    )
                    | (
                        ~models.Q(state=PipelineLaunchRequestState.SUPERSEDED)
                        & models.Q(superseded_by__isnull=True)
                    )
                ),
                name="aist_launch_request_supersede_link_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        execution_type=PipelineExecutionType.SAST,
                        dast_binding__isnull=True,
                        trigger_project_version__isnull=True,
                    )
                    # Whether trigger_project_version must be set depends on the binding's target
                    # requirement, which this constraint cannot join against; checked in clean()
                    # instead, with the binding in scope.
                    | models.Q(
                        execution_type=PipelineExecutionType.DAST,
                        dast_binding__isnull=False,
                    )
                ),
                name="aist_launch_request_execution_target_valid",
            ),
        ]

    def __str__(self):
        return f"LaunchRequest(project={self.project_id}, type={self.execution_type}, state={self.state})"

    @property
    def dispatched(self) -> bool:
        """Whether the request has been dispatched."""
        return self.state == PipelineLaunchRequestState.DISPATCHED

    def get_snapshots(self) -> LaunchRequestSnapshots:
        return LaunchRequestSnapshots.from_values(
            params=self.params_snapshot,
            capability=self.capability_snapshot,
        )

    def get_initial_launch_data_snapshot(self) -> dict:
        error_message = "initial_launch_data_snapshot must be a JSON object."
        value = validated_secret_free_json(
            self.initial_launch_data_snapshot,
            label="initial_launch_data_snapshot",
        )
        if not isinstance(value, dict):
            raise LaunchRequestSnapshotError(error_message)
        return value

    def clean(self):
        errors: dict[str, str] = {}
        try:
            self.get_snapshots()
        except LaunchRequestSnapshotError as exc:
            errors["params_snapshot"] = str(exc)
        try:
            self.get_initial_launch_data_snapshot()
        except LaunchRequestSnapshotError as exc:
            errors["initial_launch_data_snapshot"] = str(exc)

        if self.launch_config_id and self.launch_config.project_id != self.project_id:
            errors["launch_config"] = "Launch config must belong to the request project."
        elif self.launch_config_id and self.launch_config.execution_type != self.execution_type:
            errors["execution_type"] = "Request execution type must match its launch config."
        elif self.launch_config_id and self.launch_config.dast_binding_id != self.dast_binding_id:
            errors["dast_binding"] = "Request DAST binding must match its launch config."
        if self.schedule_id and self.schedule.launch_config_id != self.launch_config_id:
            errors["schedule"] = "Schedule must belong to the request launch config."
        if self.superseded_by_id and self.superseded_by.project_id != self.project_id:
            errors["superseded_by"] = "Superseding request must belong to the same project."
        if self.superseded_by_id and self.superseded_by_id == self.pk:
            errors["superseded_by"] = "A launch request cannot supersede itself."
        if self.api_token_id and self.api_token.organization_id != self.project.organization_id:
            errors["api_token"] = "PAT record must belong to the request organization."  # noqa: S105

        if self.authority_kind == PipelineLaunchAuthorityKind.PAT and not self.api_token_id:
            errors["api_token"] = "PAT authority requires a public PAT record."  # noqa: S105
        if self.api_token_id and self.authority_kind != PipelineLaunchAuthorityKind.PAT:
            errors["authority_kind"] = "A PAT record is valid only for PAT authority."
        if self.created and self.expires_at and self.expires_at <= self.created:
            errors["expires_at"] = "Expiry must be later than request creation."

        if self.execution_type == PipelineExecutionType.SAST:
            if self.dast_binding_id or self.trigger_project_version_id:
                errors["execution_type"] = "SAST launch requests cannot contain DAST execution input."
        elif self.execution_type == PipelineExecutionType.DAST:
            if not self.dast_binding_id:
                errors["dast_binding"] = "DAST launch requests require a project binding."
            else:
                errors.update(_dast_launch_errors(self))

        if errors:
            raise ValidationError(errors)


class PipelineExecutionLease(models.Model):

    """Database-backed ownership of one bounded external execution resource slot."""

    resource_key = models.CharField(max_length=255)
    slot = models.PositiveSmallIntegerField()
    request = models.ForeignKey(
        PipelineLaunchRequest,
        on_delete=models.CASCADE,
        related_name="execution_leases",
    )
    pipeline = models.ForeignKey(
        AISTPipeline,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="execution_leases",
    )
    acquired_at = models.DateTimeField(default=timezone.now)
    heartbeat_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)
    released_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["resource_key", "slot", "acquired_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["resource_key", "slot"],
                condition=models.Q(released_at__isnull=True),
                name="uniq_active_execution_lease_slot",
            ),
        ]

    def __str__(self) -> str:
        return f"ExecutionLease(resource={self.resource_key}, slot={self.slot}, request={self.request_id})"

    def clean(self):
        errors: dict[str, str] = {}
        if not self.resource_key.strip():
            errors["resource_key"] = "Resource key must not be empty."
        if self.expires_at <= self.acquired_at:
            errors["expires_at"] = "Lease expiry must be later than acquisition."
        if self.heartbeat_at < self.acquired_at:
            errors["heartbeat_at"] = "Heartbeat cannot predate acquisition."
        if self.pipeline_id and self.pipeline.project_id != self.request.project_id:
            errors["pipeline"] = "Lease pipeline and launch request must belong to the same project."
        if errors:
            raise ValidationError(errors)


class AISTProjectLaunchConfig(models.Model):

    """
    Saved launch configuration ("preset") for a specific AISTProject.

    Stores PipelineArguments-like options excluding:
      - project_id (comes from FK)
      - project_version (chosen at run time)
    """

    project = models.ForeignKey(AISTProject, on_delete=models.CASCADE, related_name="launch_configs")
    execution_type = models.CharField(
        max_length=24,
        choices=(
            (PipelineExecutionType.SAST, "SAST"),
            (PipelineExecutionType.DAST, "DAST"),
        ),
        default=PipelineExecutionType.SAST,
        db_index=True,
    )
    dast_binding = models.ForeignKey(
        DastProjectBinding,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="launch_configs",
    )
    trigger_project_version = models.ForeignKey(
        AISTProjectVersion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="dast_launch_configs",
    )

    name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")

    # PipelineArguments-equivalent options (except project_id/project_version)
    params = models.JSONField(default=dict, blank=True)

    is_default = models.BooleanField(default=False)

    created = models.DateTimeField(default=timezone.now, editable=False)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "name"], name="uniq_aist_launch_cfg_name_per_project"),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        execution_type=PipelineExecutionType.SAST,
                        dast_binding__isnull=True,
                        trigger_project_version__isnull=True,
                    )
                    # Whether trigger_project_version must be set depends on the binding's target
                    # requirement, which this constraint cannot join against; checked in clean()
                    # instead, with the binding in scope.
                    | models.Q(
                        execution_type=PipelineExecutionType.DAST,
                        dast_binding__isnull=False,
                    )
                ),
                name="aist_launch_config_execution_target_valid",
            ),
            models.UniqueConstraint(
                fields=["project"],
                condition=models.Q(is_default=True),
                name="uniq_default_launch_config_per_project",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(execution_type=PipelineExecutionType.SAST)
                    | ~models.Q(params__has_key="analyzers")
                ),
                name="aist_dast_launch_config_no_analyzers",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.project_id}:{self.name}"

    def get_launch_schedule(self) -> LaunchSchedule | None:
        try:
            return self.launch_schedule
        except LaunchSchedule.DoesNotExist:
            return None

    def clean(self):
        errors: dict[str, str] = {}
        if self.execution_type == PipelineExecutionType.SAST:
            if self.dast_binding_id:
                errors["dast_binding"] = "SAST launch config cannot select a DAST binding."
            if self.trigger_project_version_id:
                errors["trigger_project_version"] = "SAST launch config cannot select a DAST trigger version."
        elif self.execution_type == PipelineExecutionType.DAST:
            if not self.dast_binding_id:
                errors["dast_binding"] = "DAST launch config requires an explicit binding."
            else:
                errors.update(_dast_launch_errors(self))
                if not self.dast_binding.enabled:
                    errors.setdefault("dast_binding", "DAST binding must be enabled.")
            if "analyzers" in self.params:
                errors["params"] = "DAST launch config cannot contain SAST analyzers."
        else:
            errors["execution_type"] = "Launch configs support only SAST or DAST execution."
        if errors:
            raise ValidationError(errors)


class AISTLaunchConfigAction(models.Model):
    class ActionType(models.TextChoices):
        PUSH_TO_SLACK = "PUSH_TO_SLACK", "push_to_slack"
        SEND_EMAIL = "SEND_EMAIL", "send_email"
        WRITE_LOG = "WRITE_LOG", "write_log"

    launch_config = models.ForeignKey(
        AISTProjectLaunchConfig,
        on_delete=models.CASCADE,
        related_name="actions",
    )
    trigger_status = models.CharField(max_length=64, choices=AISTStatus.choices)
    action_type = models.CharField(max_length=32, choices=ActionType.choices)
    config = models.JSONField(default=dict, blank=True)

    created = models.DateTimeField(default=timezone.now, editable=False)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = []

    def __str__(self) -> str:
        return f"Action({self.launch_config_id}:{self.action_type}@{self.trigger_status})"
