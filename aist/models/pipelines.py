"""Pipelines, their tests' deduplication state, and AI triage responses."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from dojo.models import Finding, Test

from aist.launch_data import PipelineLaunchData
from aist.models.projects import AISTProject, AISTProjectVersion
from aist.models.scm import PullRequest


class AISTStatus(models.TextChoices):
    ADMITTED = "ADMITTED", "Admitted"
    EXECUTING = "EXECUTING", "Executing"
    UPLOADING_RESULTS = "UPLOADING_RESULTS", "Uploading Results"
    FINDING_POSTPROCESSING = "FINDING_POSTPROCESSING", "Finding post-processing"
    WAITING_DEDUPLICATION_TO_FINISH = "WAITING_DEDUPLICATION_TO_FINISH", "Waiting Deduplication To Finish"
    WAITING_CONFIRMATION_TO_PUSH_TO_AI = "WAITING_CONFIRMATION_TO_PUSH_TO_AI", "Waiting Confirmation To Push to AI"
    PUSH_TO_AI = "PUSH_TO_AI", "Push to AI"
    WAITING_RESULT_FROM_AI = "WAITING_RESULT_FROM_AI", "Waiting Result From AI"
    FINISHED = "FINISHED", "Finished"
    FINISHED_WITH_WARNINGS = "FINISHED_WITH_WARNINGS", "Finished With Warnings"


class PipelineExecutionType(models.TextChoices):
    SAST = "SAST", "SAST"
    DAST = "DAST", "DAST"
    MANUAL_IMPORT = "MANUAL_IMPORT", "Manual report import"


class DastExecutionOutcome(models.TextChoices):
    RUNNING = "RUNNING", "Running"
    STOP_PENDING = "STOP_PENDING", "Remote stop pending"
    TERMINAL = "TERMINAL", "Provider terminal"
    CANCELLED_BEFORE_START = "CANCELLED_BEFORE_START", "Cancelled before provider start"
    UNREACHABLE = "UNREACHABLE", "Provider unreachable"


class AISTPipeline(models.Model):
    created = models.DateTimeField(default=timezone.now, editable=False)
    updated = models.DateTimeField(auto_now=True)
    started = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    id = models.CharField(primary_key=True, max_length=64)

    project = models.ForeignKey(AISTProject, on_delete=models.PROTECT, related_name="aist_pipelines")
    project_version = models.ForeignKey(
        AISTProjectVersion,
        on_delete=models.PROTECT,
        related_name="pipelines",
        db_index=True,
        null=True, blank=True,
    )
    trigger_project_version = models.ForeignKey(
        AISTProjectVersion,
        on_delete=models.PROTECT,
        related_name="triggered_pipelines",
        db_index=True,
        null=True,
        blank=True,
        help_text="Source version that triggered a DAST run; its effective version may be resolved later.",
    )
    dast_binding = models.ForeignKey(
        "aist.DastProjectBinding",
        on_delete=models.SET_NULL,
        related_name="pipelines",
        null=True,
        blank=True,
        help_text="DAST target binding selected for this pipeline.",
    )
    execution_type = models.CharField(
        max_length=24,
        choices=PipelineExecutionType.choices,
        default=PipelineExecutionType.SAST,
        db_index=True,
    )
    status = models.CharField(max_length=64, choices=AISTStatus.choices, default=AISTStatus.ADMITTED)

    tests = models.ManyToManyField(Test, related_name="aist_pipelines", blank=True)
    launch_data = models.JSONField(default=dict, blank=True)

    run_task_id = models.CharField(max_length=64, null=True, blank=True)
    watch_dedup_task_id = models.CharField(max_length=64, null=True, blank=True)

    response_from_ai = models.JSONField(default=dict, blank=True)

    pull_request = models.ForeignKey(
        PullRequest,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="pipelines",
    )

    class Meta:
        ordering = ("-created",)
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        execution_type=PipelineExecutionType.SAST,
                        project_version__isnull=False,
                        trigger_project_version__isnull=True,
                    )
                    # No nullness condition on trigger_project_version: whether a DAST run has one
                    # depends on its binding's target requirement (dast_config.DastLaunchRequirements),
                    # which this table cannot join against. That is checked earlier, with the binding
                    # in scope, at PipelineLaunchRequest/AISTProjectLaunchConfig creation time.
                    | models.Q(execution_type=PipelineExecutionType.DAST)
                    | models.Q(
                        execution_type=PipelineExecutionType.MANUAL_IMPORT,
                        trigger_project_version__isnull=True,
                    )
                ),
                name="aist_pipeline_execution_source_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.execution_type}Pipeline[{self.id}] {self.status}"

    @property
    def finished_with_warnings(self) -> bool:
        return self.status == AISTStatus.FINISHED_WITH_WARNINGS

    @property
    def source_root(self) -> str:
        """The on-disk path of the scanned sources, as sast-pipeline reported it."""
        return PipelineLaunchData(self.launch_data).project_path

    def clean(self):
        errors = {}
        if self.project_version_id and self.project_version.project_id != self.project_id:
            errors["project_version"] = "Effective version must belong to the pipeline project."
        if self.trigger_project_version_id and self.trigger_project_version.project_id != self.project_id:
            errors["trigger_project_version"] = "Trigger version must belong to the pipeline project."

        if self.execution_type == PipelineExecutionType.SAST:
            if not self.project_version_id:
                errors["project_version"] = "SAST pipelines require an effective project version."
            if self.trigger_project_version_id:
                errors["trigger_project_version"] = "SAST pipelines cannot have a DAST trigger version."
            if self.dast_binding_id:
                errors["dast_binding"] = "SAST pipelines cannot have a DAST binding."
        elif self.execution_type == PipelineExecutionType.MANUAL_IMPORT and self.trigger_project_version_id:
            errors["trigger_project_version"] = "Manual imports cannot have a DAST trigger version."

        if self.dast_binding_id and self.dast_binding.project_id != self.project_id:
            errors["dast_binding"] = "DAST binding must belong to the pipeline project."

        if errors:
            raise ValidationError(errors)


class AISTTestMeta(models.Model):
    test = models.OneToOneField(
        Test,
        on_delete=models.CASCADE,
        related_name="aist_meta",
    )
    deduplication_complete = models.BooleanField(default=False)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["deduplication_complete"],
                name="aist_testmeta_dedup_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"AISTTestMeta(test={self.test_id}, dedup_complete={self.deduplication_complete})"


class TestDeduplicationProgress(models.Model):

    """Deduplication progress on one Test."""

    test = models.OneToOneField(
        Test, on_delete=models.CASCADE, related_name="dedupe_progress",
    )
    pending_tasks = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    deduplication_complete = models.BooleanField(default=False)
    last_progress_at = models.DateTimeField(null=True, blank=True)
    last_reconcile_at = models.DateTimeField(null=True, blank=True)
    reconcile_attempts = models.PositiveIntegerField(default=0)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["test", "pending_tasks"])]

    def __str__(self) -> str:
        return f"DeduplicationTaskGroup(test={self.test_id}, remaining={self.pending_tasks})"

    def mark_complete_if_finished(self) -> None:
        if self.pending_tasks == 0 and not self.deduplication_complete:
            self.deduplication_complete = True
            self.completed_at = timezone.now()
            self.save(update_fields=["deduplication_complete", "completed_at"])
            AISTTestMeta.objects.update_or_create(
                test_id=self.test_id,
                defaults={"deduplication_complete": True},
            )

    def refresh_pending_tasks(self) -> None:
        with transaction.atomic():
            group = (
                TestDeduplicationProgress.objects
                .select_for_update()
                .get(pk=self.pk)
            )
            now = timezone.now()
            fields_to_update = []

            if group.started_at is None:
                group.started_at = now
                fields_to_update.append("started_at")
            if group.last_progress_at is None:
                group.last_progress_at = now
                fields_to_update.append("last_progress_at")
            # test current findings
            qs_findings = Finding.objects.filter(test_id=group.test_id)

            # pending = findings, for which ProcessedFinding doesn't exist with same test_id and finding_id
            pending_qs = qs_findings.filter(
                ~models.Exists(
                    ProcessedFinding.objects.filter(
                        test_id=group.test_id,
                        finding_id=models.OuterRef("id"),
                    ),
                ),
            )

            pending = pending_qs.count()
            # completed if pending == 0 (even if 0/0)
            is_complete = (pending == 0)

            fields_to_update = []
            if group.pending_tasks != pending:
                group.pending_tasks = pending
                fields_to_update.append("pending_tasks")
                group.last_progress_at = now
                if "last_progress_at" not in fields_to_update:
                    fields_to_update.append("last_progress_at")
            if group.deduplication_complete != is_complete:
                group.deduplication_complete = is_complete
                fields_to_update.append("deduplication_complete")
                group.last_progress_at = now
                if "last_progress_at" not in fields_to_update:
                    fields_to_update.append("last_progress_at")

            if fields_to_update:
                group.save(update_fields=fields_to_update)
            AISTTestMeta.objects.update_or_create(
                test_id=group.test_id,
                defaults={"deduplication_complete": is_complete},
            )


class ProcessedFinding(models.Model):

    """A finding of a test whose deduplication has completed; each finding is recorded once."""

    test = models.ForeignKey(Test, on_delete=models.CASCADE)
    finding = models.ForeignKey(Finding, null=True, blank=True,
                                on_delete=models.SET_NULL)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["test", "finding"],
                name="uniq_processed_test_finding_not_null",
                condition=models.Q(finding__isnull=False),
            ),
        ]
        indexes = [
            # to anti JOIN work fast
            models.Index(fields=["test", "finding"]),
        ]


class AISTAIResponse(models.Model):
    class Source(models.TextChoices):
        # Default value: existing post-import Claude/n8n triage flow.
        AI_TRIAGE = "ai_triage", "AI Triage"
        # Analyzer-produced AI verdict artifacts imported as part of the SAST
        # pipeline before the regular post-import triage queue.
        AGENT_ANALYZER = "agent_analyzer", "Agent Analyzer"

    pipeline = models.ForeignKey(
        "AISTPipeline",
        on_delete=models.CASCADE,
        related_name="ai_responses",
        db_index=True,
    )
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    source = models.CharField(
        max_length=32,
        choices=Source.choices,
        default=Source.AI_TRIAGE,
        db_index=True,
    )

    class Meta:
        ordering = ["-created"]  # last one is on top

    def __str__(self):
        return f"AIResponse[{self.pipeline_id}] @ {self.created:%Y-%m-%d %H:%M:%S}"


class AISTAIFindingResponse(models.Model):
    class Verdict(models.TextChoices):
        TRUE_POSITIVE = "true_positive", "True Positive"
        FALSE_POSITIVE = "false_positive", "False Positive"
        UNCERTAIN = "uncertain", "Uncertain"

    class FixType(models.TextChoices):
        CODE_CHANGE = "code_change", "Code Change"
        CONFIG_CHANGE = "config_change", "Config Change"
        ARCHITECTURAL = "architectural", "Architectural"

    pipeline = models.ForeignKey(
        "AISTPipeline",
        on_delete=models.CASCADE,
        related_name="ai_finding_responses",
        db_index=True,
    )
    source_response = models.ForeignKey(
        "AISTAIResponse",
        on_delete=models.SET_NULL,
        related_name="finding_responses",
        null=True,
        blank=True,
    )
    finding = models.ForeignKey(
        Finding,
        on_delete=models.CASCADE,
        related_name="aist_ai_responses",
        db_index=True,
    )
    verdict = models.CharField(
        max_length=32,
        choices=Verdict.choices,
        db_index=True,
    )
    title = models.CharField(max_length=512, blank=True, default="")
    summary = models.TextField(blank=True, default="")
    references = models.JSONField(default=list, blank=True)
    epss_score = models.FloatField(null=True, blank=True)
    impact_score = models.FloatField(null=True, blank=True)
    exploitability_score = models.FloatField(null=True, blank=True)
    uncertainty_level = models.FloatField(null=True, blank=True)
    uncertainty_spread = models.FloatField(null=True, blank=True)
    exploit_code_maturity = models.CharField(max_length=64, blank=True, default="")
    fix = models.JSONField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["pipeline", "finding"],
                name="uniq_aist_ai_finding_per_pipeline",
            ),
        ]
        indexes = [
            models.Index(fields=["pipeline", "verdict"], name="aist_aistai_pipelin_3868d4_idx"),
            models.Index(fields=["pipeline", "finding"], name="aist_aistai_pipelin_6a546f_idx"),
        ]
        ordering = ["-updated"]

    def __str__(self):
        return f"AIFindingResponse[pipeline={self.pipeline_id}, finding={self.finding_id}]"
