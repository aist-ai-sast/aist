"""DAST integration state, targets, bindings, and the runtime state of DAST runs."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from dojo.models import Test

from aist.integrations.dast_config import (
    DastBindingParameters,
    DastConfigError,
    DastTargetSnapshot,
)
from aist.models.organizations import OrgIntegration, OrgIntegrationType
from aist.models.pipelines import AISTPipeline, DastExecutionOutcome, PipelineExecutionType
from aist.models.projects import AISTProjectVersion


class DastOnboardingBundleUse(models.Model):

    """
    Marks a versioned DAST onboarding bundle as consumed.

    ``integrator_public_id`` identifies one bundle export from the gateway role, not the
    gateway installation itself — re-onboarding after this record exists requires a fresh
    bundle (``dast_gateway_rotate_token: true``), not replaying the old one. Kept even if
    the ``org_integration`` it produced is later deleted, so the same bundle file cannot
    be replayed against a different organization either.
    """

    integrator_public_id = models.CharField(max_length=255, unique=True)
    org_integration = models.ForeignKey(
        OrgIntegration,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="onboarding_bundle_uses",
    )
    used_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"DastOnboardingBundleUse(integrator_public_id={self.integrator_public_id})"


class DastIntegrationValidationState(models.TextChoices):
    UNVALIDATED = "UNVALIDATED", "Unvalidated"
    PENDING_VALIDATION = "PENDING_VALIDATION", "Pending validation"
    VALIDATING = "VALIDATING", "Validating"
    READY = "READY", "Ready"
    INVALID = "INVALID", "Invalid"


class DastCapabilitySyncStatus(models.TextChoices):
    IDLE = "IDLE", "Idle"
    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"


class DastIntegrationState(models.Model):
    integration = models.OneToOneField(
        OrgIntegration,
        on_delete=models.CASCADE,
        related_name="dast_state",
    )
    validation_state = models.CharField(
        max_length=24,
        choices=DastIntegrationValidationState.choices,
        default=DastIntegrationValidationState.UNVALIDATED,
    )
    validated_at = models.DateTimeField(null=True, blank=True)
    validation_error_code = models.CharField(max_length=64, blank=True, default="")
    validation_generation = models.PositiveBigIntegerField(default=0)
    validation_task_id = models.CharField(max_length=255, blank=True, default="")
    validation_claimed_at = models.DateTimeField(null=True, blank=True)
    contract_version = models.CharField(max_length=32, blank=True, default="")
    capabilities_etag = models.CharField(max_length=255, blank=True, default="")
    capabilities_synced_at = models.DateTimeField(null=True, blank=True)
    sync_error_code = models.CharField(max_length=64, blank=True, default="")
    sync_generation = models.PositiveBigIntegerField(default=0)
    sync_task_id = models.CharField(max_length=255, blank=True, default="")
    sync_claimed_at = models.DateTimeField(null=True, blank=True)
    sync_requested_at = models.DateTimeField(null=True, blank=True)
    sync_status = models.CharField(
        max_length=16,
        choices=DastCapabilitySyncStatus.choices,
        default=DastCapabilitySyncStatus.IDLE,
    )
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"DAST state[{self.integration_id}] {self.validation_state}"

    def clean(self):
        super().clean()
        if self.integration_id and self.integration.integration_type != OrgIntegrationType.DAST:
            raise ValidationError({"integration": "DAST integration state requires a DAST integration."})


class DastTarget(models.Model):
    integration = models.ForeignKey(
        OrgIntegration,
        on_delete=models.PROTECT,
        related_name="dast_targets",
    )
    provider_id = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255)
    contract_revision = models.CharField(max_length=64)
    capability_revision = models.CharField(max_length=96)
    schema_digest = models.CharField(max_length=96)
    parameter_schema = models.JSONField(default=dict)
    provider_defaults = models.JSONField(default=dict)
    repository_keys = models.JSONField(default=list, blank=True)
    launch_requirements = models.JSONField(default=list, blank=True)
    autonomous_ready = models.BooleanField(default=False)
    is_available = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField()
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["integration", "provider_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["integration", "provider_id"],
                name="uniq_dast_target_provider_per_integration",
            ),
        ]

    def __str__(self) -> str:
        return f"DAST target[{self.integration_id}:{self.provider_id}]"

    def get_snapshot(self) -> DastTargetSnapshot:
        return DastTargetSnapshot.from_snapshot({
            "id": self.provider_id,
            "display_name": self.display_name,
            "contract_revision": self.contract_revision,
            "capability_revision": self.capability_revision,
            "schema_digest": self.schema_digest,
            "parameter_schema": self.parameter_schema,
            "defaults": self.provider_defaults,
            "repository_keys": self.repository_keys,
            "launch_requirements": self.launch_requirements,
            "autonomous_ready": self.autonomous_ready,
        })

    def clean(self):
        super().clean()
        errors = {}
        if self.integration_id and self.integration.integration_type != OrgIntegrationType.DAST:
            errors["integration"] = "DAST targets require a DAST integration."
        if self.pk:
            original_integration_id = type(self).objects.filter(pk=self.pk).values_list(
                "integration_id",
                flat=True,
            ).first()
            if original_integration_id is not None and original_integration_id != self.integration_id:
                errors["integration"] = "A discovered DAST target cannot be moved to another integration."
        try:
            self.get_snapshot()
        except DastConfigError as exc:
            errors["parameter_schema"] = str(exc)
        if errors:
            raise ValidationError(errors)


class DastLaunchViolation(models.TextChoices):
    OTHER_PROJECT = "OTHER_PROJECT", "The DAST binding must belong to the launch project."
    TRIGGER_REQUIRED = "TRIGGER_REQUIRED", "This target requires a Git trigger version."
    TRIGGER_NOT_ACCEPTED = (
        "TRIGGER_NOT_ACCEPTED",
        "This target has no repository requirement; clear the trigger version.",
    )
    TRIGGER_OTHER_PROJECT = "TRIGGER_OTHER_PROJECT", "The trigger version must belong to the launch project."
    TRIGGER_NOT_GIT = "TRIGGER_NOT_GIT", "The trigger version must be a Git branch or Git hash."

    @property
    def concerns_trigger(self) -> bool:
        return self is not DastLaunchViolation.OTHER_PROJECT


def launch_violation_errors(
    violations: tuple[DastLaunchViolation, ...], *, binding_field: str, trigger_field: str,
) -> dict[str, str]:
    """``violations`` as field errors: the first violation of each field, keyed by the caller's field names."""
    errors: dict[str, str] = {}
    for violation in violations:
        errors.setdefault(trigger_field if violation.concerns_trigger else binding_field, violation.label)
    return errors


class DastProjectBinding(models.Model):
    project = models.ForeignKey(
        "AISTProject",
        on_delete=models.CASCADE,
        related_name="dast_bindings",
    )
    target = models.ForeignKey(
        DastTarget,
        on_delete=models.PROTECT,
        related_name="project_bindings",
    )
    source_repo_key = models.CharField(max_length=128, blank=True, default="")
    enabled = models.BooleanField(default=True)
    parameter_snapshot = models.JSONField(default=dict, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project", "target"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "target"],
                name="uniq_dast_binding_project_target",
            ),
        ]

    def __str__(self) -> str:
        return f"DAST binding[{self.project_id}:{self.target_id}]"

    def get_parameters(self) -> DastBindingParameters:
        return DastBindingParameters.from_snapshot(
            self.parameter_snapshot,
            target=self.target.get_snapshot(),
        )

    @property
    def requires_source_repository(self) -> bool:
        """
        Whether this binding's target scenario declares a REPOSITORY_TRIGGER requirement.

        The single accessor every other DAST module asks instead of re-deriving the answer from
        `repository_keys`/`source_repo_key` truthiness; see `aist/integrations/dast_config.py`'s
        `DastLaunchRequirements`.
        """
        return self.target.get_snapshot().launch_requirements.requires_repository()

    def launch_violations(
        self, *, project_id: int, trigger_version: AISTProjectVersion | None,
    ) -> tuple[DastLaunchViolation, ...]:
        """
        Every reason a launch for ``project_id`` triggered by ``trigger_version`` may not use this binding.

        The binding must belong to that project, and the trigger must be a Git version of that
        project exactly when the target requires a source repository; an empty result means it may.
        """
        violations = []
        if self.project_id != project_id:
            violations.append(DastLaunchViolation.OTHER_PROJECT)
        if not self.requires_source_repository:
            if trigger_version is not None:
                violations.append(DastLaunchViolation.TRIGGER_NOT_ACCEPTED)
        elif trigger_version is None:
            violations.append(DastLaunchViolation.TRIGGER_REQUIRED)
        else:
            if trigger_version.project_id != project_id:
                violations.append(DastLaunchViolation.TRIGGER_OTHER_PROJECT)
            if not trigger_version.is_git():
                violations.append(DastLaunchViolation.TRIGGER_NOT_GIT)
        return tuple(violations)

    def clean(self):
        super().clean()
        errors = {}
        if self.target_id:
            integration = self.target.integration
            if integration.integration_type != OrgIntegrationType.DAST:
                errors["target"] = "Binding target must belong to a DAST integration."
            elif not integration.is_active:
                errors["target"] = "Binding target must belong to the active DAST integration."
            elif self.project_id and self.project.organization_id != integration.organization_id:
                errors["target"] = "Binding project and DAST target must belong to the same organization."
            if self.requires_source_repository:
                if self.source_repo_key not in self.target.get_snapshot().repository_keys:
                    errors["source_repo_key"] = "Source repository key is not advertised by the DAST target."
            elif self.source_repo_key:
                errors["source_repo_key"] = "Source repository key is not accepted by a target with no repository requirement."
            try:
                self.get_parameters()
            except DastConfigError as exc:
                errors["parameter_snapshot"] = str(exc)
        if errors:
            raise ValidationError(errors)


class DastExecutionState(models.Model):

    """Typed provider runtime state kept outside the common pipeline row."""

    pipeline = models.OneToOneField(
        AISTPipeline,
        on_delete=models.CASCADE,
        related_name="dast_execution_state",
    )
    run_id = models.CharField(max_length=255, null=True, blank=True)
    log_cursor = models.PositiveBigIntegerField(default=0)
    outcome = models.CharField(
        max_length=32,
        choices=DastExecutionOutcome.choices,
        blank=True,
        default="",
    )
    # When the provider last delivered a run id or new log events -- the run's sign of life.
    # NULL means "no baseline yet"; a reader must never read it as "stalled".
    last_progress_at = models.DateTimeField(null=True, blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    recovery_checkpoint = models.JSONField(default=dict, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.pipeline.execution_type != PipelineExecutionType.DAST:
            raise ValidationError({"pipeline": "DAST execution state requires a DAST pipeline."})


class DastQueueSnapshot(models.Model):

    """
    The last queue payload read for one pipeline's DAST run.

    The queue itself belongs to the provider, and reaching it needs the Docker socket and the VPN
    route that only a worker has. This is where a worker leaves what it read, so the page can be
    served without any process that renders it talking to a gateway.

    Keeping it durable rather than cached buys two things. A finished run's queue stays readable as
    the record of what that run covered and what it did not, even once the gateway stops serving it.
    And a demo install can be bootstrapped with a snapshot of its own: the page renders whatever
    snapshot exists and never asks who wrote it.
    """

    SOURCE_GATEWAY = "gateway"
    SOURCE_BOOTSTRAP = "bootstrap"

    pipeline = models.OneToOneField(
        AISTPipeline,
        on_delete=models.CASCADE,
        related_name="dast_queue_snapshot",
    )
    # The run this snapshot describes. A snapshot of a different run than the pipeline now names is
    # stale by identity, not by age.
    run_id = models.CharField(max_length=255, blank=True, default="")
    descriptor = models.JSONField(default=dict, blank=True)
    page = models.JSONField(default=dict, blank=True)
    # The gateway's entity-tag for the page, replayed as If-None-Match so an unchanged queue costs
    # a 304 rather than a page.
    etag = models.CharField(max_length=128, blank=True, default="")
    descriptor_digest = models.CharField(max_length=128, blank=True, default="")
    # Why there is nothing to show, when that is the answer: an old run the gateway no longer
    # serves is an expected state, not a failure to hide.
    unavailable_code = models.CharField(max_length=64, blank=True, default="")
    unavailable_detail = models.TextField(blank=True, default="")
    source = models.CharField(max_length=32, default=SOURCE_GATEWAY)
    fetched_at = models.DateTimeField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"DastQueueSnapshot(pipeline={self.pipeline_id}, run={self.run_id})"


# Column name -> attribute on the validated coverage / token-usage value objects. The two
# names differ only where a column reads better than the wire counter it came from.
_DAST_COVERAGE_COUNT_COLUMNS = ("discovered", "reachable", "analysed", "planned")


_DAST_COVERAGE_NAME_COLUMNS = ("analysed_names", "beyond_plan_names")


_DAST_TOKEN_COUNT_COLUMNS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "thinking_tokens": "thinking_tokens",
    "cache_creation_tokens": "cache_creation_tokens",
    "cache_read_tokens": "cache_read_tokens",
    "model_calls": "calls",
}


class DastRunMetadataManager(models.Manager):

    """Persists one validated ``dast_run_metadata`` block; the only value-object-to-column map."""

    @staticmethod
    def _reported(block, attribute: str):
        """Read one optional attribute off a block the report may not have carried at all."""
        return None if block is None else getattr(block, attribute)

    @classmethod
    def _reported_names(cls, coverage, attribute: str) -> list[str] | None:
        names = cls._reported(coverage, attribute)
        return None if names is None else list(names)

    @classmethod
    def _reported_buckets(cls, usage, attribute: str) -> list[dict] | None:
        buckets = cls._reported(usage, attribute)
        return None if buckets is None else [bucket.as_wire() for bucket in buckets]

    def _column_text(self, column: str, value: str | None) -> str | None:
        """Fit descriptive text to the actual database column without duplicating its schema."""
        if value is None:
            return None
        maximum = self.model._meta.get_field(column).max_length
        return value if maximum is None or len(value) <= maximum else None

    def columns_from_report(self, metadata) -> dict:
        """Flatten one validated metadata block onto this table's columns."""
        coverage = metadata.coverage
        usage = metadata.token_usage
        total = self._reported(usage, "total")
        return {
            "run_id": metadata.run_id,
            "target_id": metadata.target_id,
            **{
                column: self._column_text(column, getattr(metadata, column))
                for column in ("stand_id", "product_family", "tier", "run_type", "target_host")
            },
            "scan_started": metadata.scan_started,
            "scan_finished": metadata.scan_finished,
            "delivery_quality": metadata.delivery_quality,
            "audit_state": metadata.audit_state,
            "findings_complete": metadata.findings_complete,
            "source_verified": None,
            "operator_actions_persisted": metadata.operator_actions_persisted,
            "operator_actions": (
                None if metadata.operator_actions is None
                else [row.as_wire() for row in metadata.operator_actions]
            ),
            "operator_actions_total": metadata.operator_actions_total,
            "operator_actions_truncated": metadata.operator_actions_truncated,
            "excluded_findings": (
                None if metadata.excluded_findings is None
                else [row.as_wire() for row in metadata.excluded_findings]
            ),
            "excluded_findings_total": metadata.excluded_findings_total,
            "excluded_findings_truncated": metadata.excluded_findings_truncated,
            "coverage_unit": self._column_text("coverage_unit", self._reported(coverage, "unit")),
            **{column: self._reported(coverage, column) for column in _DAST_COVERAGE_COUNT_COLUMNS},
            **{column: self._reported_names(coverage, column) for column in _DAST_COVERAGE_NAME_COLUMNS},
            **{
                column: self._reported(total, attribute)
                for column, attribute in _DAST_TOKEN_COUNT_COLUMNS.items()
            },
            "token_by_phase": self._reported_buckets(usage, "by_phase"),
            "token_by_agent_type": self._reported_buckets(usage, "by_agent_type"),
            "token_economy": (
                None if self._reported(usage, "economy") is None
                else usage.economy.as_wire()
            ),
            "token_accounting_consistent": self._reported(usage, "accounting_consistent"),
        }

    def build_from_report(self, metadata) -> DastRunMetadata:
        """
        An unsaved row for a report that has not been imported yet.

        Lets the import preview run the same read derivations the pipeline list runs after the
        import, so the operator sees exactly the numbers that will appear.
        """
        return self.model(**self.columns_from_report(metadata))

    def upsert_from_report(self, *, pipeline_id: str, report) -> DastRunMetadata:
        """
        Write the accepted report's run metadata, replacing any earlier write for this pipeline.

        Idempotent by construction: finalization calls this before its own already-finalized
        short circuit, so redelivering the same report rewrites identical data and a pipeline
        finalized before this table existed gains its row.
        """
        row, _created = self.update_or_create(
            pipeline_id=pipeline_id,
            defaults=self.columns_from_report(report.run_metadata),
        )
        return row


class DastRunMetadata(models.Model):

    """
    Provider-reported run metadata carried by one accepted DAST report.

    Every descriptive column is nullable. Missing, empty, or unsupported values stay NULL;
    zero and false remain values because they carry information.

    Readers may treat NULL as the only empty state, but that comes from the writer rather than
    from the column: :meth:`DastRunMetadataManager.columns_from_report` is the only thing that
    fills this table, and it stores either None or a value the report validator has already
    refused to accept blank. ``blank=True`` is kept because the fields genuinely are optional,
    so a form would be wrong to demand them; this model is not registered in the admin, and a
    future form-based writer would have to preserve that invariant itself.

    Distinct from :class:`DastExecutionState`, which is provider *runtime* state written
    before any report exists so a run can be polled and resumed. This row is *reported*
    state, written once when a report crosses the trust boundary, and it is the same row for
    an autonomous run and an operator upload — which is why the report's own identity
    (``run_id``, ``target_id``, ``stand_id``) lives here rather than being read from two
    different places depending on the pipeline's execution type.
    """

    pipeline = models.OneToOneField(
        AISTPipeline,
        on_delete=models.CASCADE,
        related_name="dast_run_metadata",
    )

    run_id = models.CharField(max_length=255)
    target_id = models.CharField(max_length=255)
    stand_id = models.CharField(max_length=255, null=True, blank=True)

    product_family = models.CharField(max_length=64, null=True, blank=True)
    tier = models.CharField(max_length=64, null=True, blank=True)
    run_type = models.CharField(max_length=64, null=True, blank=True)
    target_host = models.CharField(max_length=255, null=True, blank=True)
    scan_started = models.DateTimeField(null=True, blank=True)
    scan_finished = models.DateTimeField(null=True, blank=True)

    delivery_quality = models.CharField(max_length=16, null=True, blank=True)
    audit_state = models.CharField(max_length=16, null=True, blank=True)
    findings_complete = models.BooleanField(null=True, blank=True)
    # Transport provenance: NULL for manual uploads, never inferred from report metadata.
    source_verified = models.BooleanField(null=True, blank=True)
    operator_actions_persisted = models.BooleanField(null=True, blank=True)
    operator_actions = models.JSONField(null=True, blank=True, default=None)
    operator_actions_total = models.PositiveIntegerField(null=True, blank=True)
    operator_actions_truncated = models.BooleanField(null=True, blank=True)
    excluded_findings = models.JSONField(null=True, blank=True, default=None)
    excluded_findings_total = models.PositiveIntegerField(null=True, blank=True)
    excluded_findings_truncated = models.BooleanField(null=True, blank=True)

    coverage_unit = models.CharField(max_length=64, null=True, blank=True)
    discovered = models.PositiveIntegerField(null=True, blank=True)
    reachable = models.PositiveIntegerField(null=True, blank=True)
    analysed = models.PositiveIntegerField(null=True, blank=True)
    planned = models.PositiveIntegerField(null=True, blank=True)
    analysed_names = models.JSONField(null=True, blank=True, default=None)
    beyond_plan_names = models.JSONField(null=True, blank=True, default=None)

    input_tokens = models.BigIntegerField(null=True, blank=True)
    output_tokens = models.BigIntegerField(null=True, blank=True)
    thinking_tokens = models.BigIntegerField(null=True, blank=True)
    cache_creation_tokens = models.BigIntegerField(null=True, blank=True)
    cache_read_tokens = models.BigIntegerField(null=True, blank=True)
    model_calls = models.PositiveIntegerField(null=True, blank=True)
    token_by_phase = models.JSONField(null=True, blank=True, default=None)
    token_by_agent_type = models.JSONField(null=True, blank=True, default=None)
    token_economy = models.JSONField(null=True, blank=True, default=None)
    # False when a breakdown could be compared against the reported total and disagreed with
    # it. Recorded and surfaced rather than rejected: both sides are individually well-formed,
    # and a report full of real findings must not be lost to an accounting mismatch.
    token_accounting_consistent = models.BooleanField(null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    objects = DastRunMetadataManager()

    def __str__(self) -> str:
        return f"DastRunMetadata(pipeline={self.pipeline_id}, run={self.run_id})"

    def clean(self):
        if self.pipeline.execution_type not in {PipelineExecutionType.DAST, PipelineExecutionType.MANUAL_IMPORT}:
            raise ValidationError({"pipeline": "DAST run metadata requires a DAST or manual-import pipeline."})


class DastTestBinding(models.Model):

    """Persist the destination namespace of a DAST test independently of its launching pipeline."""

    test = models.OneToOneField(Test, on_delete=models.CASCADE, related_name="dast_binding_scope")
    binding = models.ForeignKey(DastProjectBinding, on_delete=models.SET_NULL, null=True)
    project_version = models.ForeignKey(AISTProjectVersion, on_delete=models.SET_NULL, null=True)
    repository = models.TextField(null=True)
