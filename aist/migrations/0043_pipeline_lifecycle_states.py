import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


def _replace_trigger_statuses(payload):
    if not isinstance(payload, dict):
        return payload
    changed = False
    result = dict(payload)
    for key in ("launch_config_actions", "one_off_actions"):
        actions = result.get(key)
        if not isinstance(actions, list):
            continue
        normalized = []
        for action in actions:
            normalized_action = action
            if isinstance(action, dict) and action.get("trigger_status") == "SAST_LAUNCHED":
                normalized_action = {**action, "trigger_status": "EXECUTING"}
                changed = True
            normalized.append(normalized_action)
        result[key] = normalized
    return result if changed else payload


def migrate_pipeline_states(apps, schema_editor):
    del schema_editor
    pipeline_model = apps.get_model("aist", "AISTPipeline")
    action_model = apps.get_model("aist", "AISTLaunchConfigAction")
    request_model = apps.get_model("aist", "PipelineLaunchRequest")

    action_model.objects.filter(trigger_status="SAST_LAUNCHED").update(trigger_status="EXECUTING")
    pipeline_model.objects.filter(status="SAST_LAUNCHED").update(status="EXECUTING")

    admitted_ids = request_model.objects.filter(
        state__in=("PLANNED", "PUBLISHED"),
        pipeline_id__isnull=False,
    ).values_list("pipeline_id", flat=True)
    pipeline_model.objects.filter(
        pk__in=admitted_ids,
        status="FINISHED",
        run_task_id__isnull=False,
    ).update(status="ADMITTED", started=None, finished_at=None)

    executing_ids = request_model.objects.filter(
        state="DISPATCHED",
        pipeline_id__isnull=False,
    ).values_list("pipeline_id", flat=True)
    pipeline_model.objects.filter(
        pk__in=executing_ids,
        status="FINISHED",
        run_task_id__isnull=False,
    ).update(status="EXECUTING", finished_at=None)

    # A pre-0042 manual import has no launch request, but an owned task id is
    # durable evidence that the old FINISHED value was only a pre-start sentinel.
    pipeline_model.objects.filter(
        status="FINISHED",
        run_task_id__isnull=False,
    ).update(status="EXECUTING", finished_at=None)

    terminal = pipeline_model.objects.filter(status__in=("FINISHED", "FINISHED_WITH_WARNINGS"))
    terminal.update(finished_at=models.F("updated"))
    pipeline_model.objects.filter(status="ADMITTED").update(started=None, finished_at=None)

    for pipeline in pipeline_model.objects.exclude(launch_data={}).only("pk", "launch_data").iterator():
        normalized = _replace_trigger_statuses(pipeline.launch_data)
        if normalized is not pipeline.launch_data:
            pipeline_model.objects.filter(pk=pipeline.pk).update(launch_data=normalized)


def reverse_pipeline_states(apps, schema_editor):
    del schema_editor
    pipeline_model = apps.get_model("aist", "AISTPipeline")
    action_model = apps.get_model("aist", "AISTLaunchConfigAction")
    action_model.objects.filter(trigger_status="EXECUTING").update(trigger_status="SAST_LAUNCHED")
    pipeline_model.objects.filter(status="EXECUTING").update(status="SAST_LAUNCHED")
    pipeline_model.objects.filter(status="ADMITTED").update(status="FINISHED")


STATUS_CHOICES = [
    ("ADMITTED", "Admitted"),
    ("EXECUTING", "Executing"),
    ("UPLOADING_RESULTS", "Uploading Results"),
    ("FINDING_POSTPROCESSING", "Finding post-processing"),
    ("WAITING_DEDUPLICATION_TO_FINISH", "Waiting Deduplication To Finish"),
    ("WAITING_CONFIRMATION_TO_PUSH_TO_AI", "Waiting Confirmation To Push to AI"),
    ("PUSH_TO_AI", "Push to AI"),
    ("WAITING_RESULT_FROM_AI", "Waiting Result From AI"),
    ("FINISHED", "Finished"),
    ("FINISHED_WITH_WARNINGS", "Finished With Warnings"),
]


def reset_dast_launch_control_and_backfill(apps, schema_editor):
    del schema_editor
    pipeline_model = apps.get_model("aist", "AISTPipeline")
    state_model = apps.get_model("aist", "DastExecutionState")
    request_model = apps.get_model("aist", "PipelineLaunchRequest")
    config_model = apps.get_model("aist", "AISTProjectLaunchConfig")
    schedule_model = apps.get_model("aist", "LaunchSchedule")

    for pipeline in pipeline_model.objects.filter(execution_type="DAST").iterator(chunk_size=500):
        state_model.objects.update_or_create(
            pipeline_id=pipeline.pk,
            defaults={
                "run_id": pipeline.external_run_id,
                "log_cursor": pipeline.external_log_cursor,
                "outcome": pipeline.external_execution_outcome,
                "deadline": pipeline.external_execution_deadline,
                "cancel_requested_at": pipeline.external_cancel_requested_at,
            },
        )

    # The previous DAST preset/request contract did not carry the source trigger
    # required by planning. Preserve pipelines and findings, but remove only the
    # launch-control rows which cannot satisfy the new invariant.
    request_model.objects.filter(execution_type="DAST").delete()
    config_model.objects.filter(execution_type="DAST").delete()

    for project_id in (
        config_model.objects.filter(is_default=True)
        .values_list("project_id", flat=True)
        .distinct()
    ):
        defaults = list(
            config_model.objects.filter(project_id=project_id, is_default=True)
            .order_by("-updated", "-pk")
            .values_list("pk", flat=True),
        )
        if len(defaults) > 1:
            config_model.objects.filter(pk__in=defaults[1:]).update(is_default=False)

    # Existing schedules are claimed once after deployment and then receive their
    # canonical next tick from the scheduler service.
    schedule_model.objects.filter(enabled=True).update(next_run_at=timezone.now())


def reverse_runtime_state(apps, schema_editor):
    del schema_editor
    pipeline_model = apps.get_model("aist", "AISTPipeline")
    state_model = apps.get_model("aist", "DastExecutionState")
    for state in state_model.objects.select_related("pipeline").iterator(chunk_size=500):
        pipeline_model.objects.filter(pk=state.pipeline_id).update(
            external_run_id=state.run_id,
            external_log_cursor=state.log_cursor,
            external_execution_outcome=state.outcome,
            external_execution_deadline=state.deadline,
            external_cancel_requested_at=state.cancel_requested_at,
        )


CREATE_RELATION_TRIGGERS = """
CREATE OR REPLACE FUNCTION aist_validate_launch_config_source()
RETURNS trigger AS $$
DECLARE
    version_row record;
BEGIN
    IF NEW.execution_type = 'DAST' THEN
        SELECT project_id, version_type INTO version_row
          FROM aist_aistprojectversion
         WHERE id = NEW.trigger_project_version_id
         FOR SHARE;
        IF version_row.project_id IS NULL OR version_row.project_id IS DISTINCT FROM NEW.project_id THEN
            RAISE EXCEPTION 'DAST launch config trigger must belong to the same project'
                USING ERRCODE = '23514', CONSTRAINT = 'aist_launch_config_trigger_project_match';
        END IF;
        IF version_row.version_type NOT IN ('GIT_BRANCH', 'GIT_HASH') THEN
            RAISE EXCEPTION 'DAST launch config trigger must be a Git version'
                USING ERRCODE = '23514', CONSTRAINT = 'aist_launch_config_trigger_git';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER aist_launch_config_source_invariants
BEFORE INSERT OR UPDATE OF execution_type, project_id, trigger_project_version_id
ON aist_aistprojectlaunchconfig
FOR EACH ROW EXECUTE FUNCTION aist_validate_launch_config_source();

CREATE OR REPLACE FUNCTION aist_validate_launch_request_source()
RETURNS trigger AS $$
DECLARE
    binding_row record;
    version_row record;
BEGIN
    IF NEW.execution_type = 'DAST' THEN
        SELECT project_id INTO binding_row
          FROM aist_dastprojectbinding
         WHERE id = NEW.dast_binding_id
         FOR SHARE;
        SELECT project_id, version_type INTO version_row
          FROM aist_aistprojectversion
         WHERE id = NEW.trigger_project_version_id
         FOR SHARE;
        IF binding_row.project_id IS NULL OR binding_row.project_id IS DISTINCT FROM NEW.project_id THEN
            RAISE EXCEPTION 'DAST launch request binding must belong to the same project'
                USING ERRCODE = '23514', CONSTRAINT = 'aist_launch_request_binding_project_match';
        END IF;
        IF version_row.project_id IS NULL OR version_row.project_id IS DISTINCT FROM NEW.project_id THEN
            RAISE EXCEPTION 'DAST launch request trigger must belong to the same project'
                USING ERRCODE = '23514', CONSTRAINT = 'aist_launch_request_trigger_project_match';
        END IF;
        IF version_row.version_type NOT IN ('GIT_BRANCH', 'GIT_HASH') THEN
            RAISE EXCEPTION 'DAST launch request trigger must be a Git version'
                USING ERRCODE = '23514', CONSTRAINT = 'aist_launch_request_trigger_git';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER aist_launch_request_source_invariants
BEFORE INSERT OR UPDATE OF execution_type, project_id, dast_binding_id, trigger_project_version_id
ON aist_pipelinelaunchrequest
FOR EACH ROW EXECUTE FUNCTION aist_validate_launch_request_source();
"""

DROP_RELATION_TRIGGERS = """
DROP TRIGGER IF EXISTS aist_launch_request_source_invariants ON aist_pipelinelaunchrequest;
DROP FUNCTION IF EXISTS aist_validate_launch_request_source();
DROP TRIGGER IF EXISTS aist_launch_config_source_invariants ON aist_aistprojectlaunchconfig;
DROP FUNCTION IF EXISTS aist_validate_launch_config_source();
"""


class Migration(migrations.Migration):

    dependencies = [("aist", "0042_dast_generic_execution")]

    operations = [
        migrations.AlterField(
            model_name="aistpipeline",
            name="started",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="aistpipeline",
            name="finished_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(migrate_pipeline_states, reverse_pipeline_states),
        migrations.AlterField(
            model_name="aistpipeline",
            name="status",
            field=models.CharField(choices=STATUS_CHOICES, default="ADMITTED", max_length=64),
        ),
        migrations.AlterField(
            model_name="aistlaunchconfigaction",
            name="trigger_status",
            field=models.CharField(choices=STATUS_CHOICES, max_length=64),
        ),
        migrations.AddField(
            model_name="launchschedule",
            name="next_run_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="launchschedule",
            name="last_attempt_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="launchschedule",
            name="last_error_code",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="launchschedule",
            name="last_error_detail",
            field=models.CharField(blank=True, default="", max_length=512),
        ),
        migrations.AddIndex(
            model_name="launchschedule",
            index=models.Index(fields=["enabled", "next_run_at"], name="aist_schedule_due_idx"),
        ),
        migrations.AddField(
            model_name="aistprojectlaunchconfig",
            name="trigger_project_version",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="dast_launch_configs",
                to="aist.aistprojectversion",
            ),
        ),
        migrations.AlterField(
            model_name="pipelinelaunchrequest",
            name="launch_config",
            field=models.ForeignKey(
                blank=True,
                help_text="Launch config used to build pipeline_args snapshot.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="launch_requests",
                to="aist.aistprojectlaunchconfig",
            ),
        ),
        migrations.CreateModel(
            name="DastExecutionState",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("run_id", models.CharField(blank=True, max_length=255, null=True)),
                ("log_cursor", models.PositiveBigIntegerField(default=0)),
                (
                    "outcome",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("RUNNING", "Running"),
                            ("STOP_PENDING", "Remote stop pending"),
                            ("TERMINAL", "Provider terminal"),
                            ("CANCELLED_BEFORE_START", "Cancelled before provider start"),
                            ("UNREACHABLE", "Provider unreachable"),
                        ],
                        default="",
                        max_length=32,
                    ),
                ),
                ("deadline", models.DateTimeField(blank=True, null=True)),
                ("cancel_requested_at", models.DateTimeField(blank=True, null=True)),
                ("recovery_checkpoint", models.JSONField(blank=True, default=dict)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "pipeline",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="dast_execution_state",
                        to="aist.aistpipeline",
                    ),
                ),
            ],
        ),
        migrations.RunPython(reset_dast_launch_control_and_backfill, reverse_runtime_state),
        # Flush deferred cascade/FK trigger events created by the legacy DAST
        # cleanup before altering constraints on the same PostgreSQL tables.
        migrations.RunSQL("SET CONSTRAINTS ALL IMMEDIATE", migrations.RunSQL.noop),
        migrations.RemoveConstraint(
            model_name="aistpipeline",
            name="aist_pipeline_dast_control_fields_valid",
        ),
        migrations.RemoveField(model_name="aistpipeline", name="external_run_id"),
        migrations.RemoveField(model_name="aistpipeline", name="external_log_cursor"),
        migrations.RemoveField(model_name="aistpipeline", name="external_execution_outcome"),
        migrations.RemoveField(model_name="aistpipeline", name="external_execution_deadline"),
        migrations.RemoveField(model_name="aistpipeline", name="external_cancel_requested_at"),
        migrations.RemoveConstraint(
            model_name="aistprojectlaunchconfig",
            name="aist_launch_config_execution_target_valid",
        ),
        migrations.AddConstraint(
            model_name="aistprojectlaunchconfig",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(execution_type="SAST", dast_binding__isnull=True, trigger_project_version__isnull=True)
                    | models.Q(execution_type="DAST", dast_binding__isnull=False, trigger_project_version__isnull=False)
                ),
                name="aist_launch_config_execution_target_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="aistprojectlaunchconfig",
            constraint=models.UniqueConstraint(
                fields=("project",),
                condition=models.Q(is_default=True),
                name="uniq_default_launch_config_per_project",
            ),
        ),
        migrations.AddConstraint(
            model_name="pipelinelaunchrequest",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(execution_type="SAST", dast_binding__isnull=True, trigger_project_version__isnull=True)
                    | models.Q(execution_type="DAST", dast_binding__isnull=False, trigger_project_version__isnull=False)
                ),
                name="aist_launch_request_execution_target_valid",
            ),
        ),
        migrations.RunSQL(CREATE_RELATION_TRIGGERS, DROP_RELATION_TRIGGERS),
    ]
