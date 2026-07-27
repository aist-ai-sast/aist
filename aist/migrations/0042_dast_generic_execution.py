"""
Generic launch execution and DAST integration schema.

Squashed from the sixteen incremental migrations authored while the feature was still under
development. Production is at 0041 and never applied any of them, so the intermediate states
carry no operational value. The three data steps below do: they classify and backfill rows that
already exist in production, and their position relative to the surrounding schema operations
is load-bearing.
"""

import uuid
from datetime import timedelta

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.db.models import F


def classify_existing_pipelines(apps, schema_editor):
    """Split pre-existing pipelines into SAST runs and fabricated manual imports."""
    del schema_editor
    aist_pipeline = apps.get_model("aist", "AISTPipeline")

    manual_import_ids = []
    invalid_pipeline_ids = []
    for pipeline in aist_pipeline.objects.only("id", "launch_data", "project_version_id").iterator():
        launch_data = pipeline.launch_data if isinstance(pipeline.launch_data, dict) else {}
        if launch_data.get("source") == "manual_import":
            manual_import_ids.append(pipeline.pk)
        elif pipeline.project_version_id is None:
            invalid_pipeline_ids.append(pipeline.pk)

    if invalid_pipeline_ids:
        preview = ", ".join(invalid_pipeline_ids[:20])
        suffix = f" and {len(invalid_pipeline_ids) - 20} more" if len(invalid_pipeline_ids) > 20 else ""
        msg = (
            "Cannot classify existing pipelines without project_version as SAST or manual imports: "
            f"{preview}{suffix}"
        )
        raise RuntimeError(msg)

    if manual_import_ids:
        aist_pipeline.objects.filter(pk__in=manual_import_ids).update(execution_type="MANUAL_IMPORT")


def restore_existing_pipelines_as_sast(apps, schema_editor):
    del schema_editor
    apps.get_model("aist", "AISTPipeline").objects.update(execution_type="SAST")


def backfill_launch_requests(apps, schema_editor):
    """Give every pre-existing queue row a deterministic generic launch-request identity."""
    del schema_editor
    launch_request = apps.get_model("aist", "PipelineLaunchRequest")
    duplicate_pipeline_ids = (
        launch_request.objects.exclude(pipeline_id=None)
        .values("pipeline_id")
        .annotate(count=models.Count("id"))
        .filter(count__gt=1)
        .values_list("pipeline_id", flat=True)
    )
    if duplicate_pipeline_ids.exists():
        msg = "Cannot make launch request pipeline relation unique: duplicate pipeline links exist."
        raise RuntimeError(msg)

    for request in launch_request.objects.select_related("launch_config").iterator(chunk_size=500):
        is_scheduled = request.schedule_id is not None
        request.origin = "SCHEDULE" if is_scheduled else "MANUAL"
        request.authority_kind = "SCHEDULE" if is_scheduled else "USER"
        request.execution_type = "SAST"
        request.state = "DISPATCHED" if request.dispatched else "PENDING"
        request.params_snapshot = dict(request.launch_config.params) if request.launch_config_id else {}
        request.capability_snapshot = {}
        request.not_before = request.created
        request.task_id = uuid.uuid4()
        request.save(
            update_fields=[
                "origin",
                "authority_kind",
                "execution_type",
                "state",
                "params_snapshot",
                "capability_snapshot",
                "not_before",
                "task_id",
            ],
        )


def backfill_pending_request_expiry(apps, schema_editor):
    del schema_editor
    launch_request = apps.get_model("aist", "PipelineLaunchRequest")
    launch_request.objects.filter(
        state="PENDING",
        expires_at__isnull=True,
    ).update(expires_at=F("created") + timedelta(hours=24))


class Migration(migrations.Migration):

    # RenameField for launchschedule.max_concurrent_per_worker (H13): the field itself was
    # added by 0005, long before this migration existed, but 0042 has not been pushed/applied
    # anywhere yet, so folding the rename in here (instead of a trailing 0058) keeps the
    # not-yet-shipped schema changes in one place. Django only requires that a migration run
    # after the field's origin, not that it live in the same file.
    dependencies = [
        ("aist", "0041_tenant_integrity_invariants"),
        ("dojo", "0260_alter_engagement_status_alter_engagementevent_status"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RenameField(
            model_name="launchschedule",
            old_name="max_concurrent_per_worker",
            new_name="max_concurrent_runs",
        ),
        migrations.AlterField(
            model_name="launchschedule",
            name="max_concurrent_runs",
            field=models.PositiveIntegerField(
                default=1,
                help_text="Maximum number of concurrent pipeline runs this schedule's own resource slot allows.",
            ),
        ),
        migrations.AddField(
            model_name="aistpipeline",
            name="execution_type",
            field=models.CharField(choices=[("SAST", "SAST"), ("DAST", "DAST"), ("MANUAL_IMPORT", "Manual report import")], db_index=True, default="SAST", max_length=24),
        ),
        migrations.AddField(
            model_name="aistpipeline",
            name="external_log_cursor",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="aistpipeline",
            name="external_run_id",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="aistpipeline",
            name="trigger_project_version",
            field=models.ForeignKey(blank=True, help_text="Source version that triggered a DAST run; its effective version may be resolved later.", null=True, on_delete=django.db.models.deletion.PROTECT, related_name="triggered_pipelines", to="aist.aistprojectversion"),
        ),
        migrations.RunPython(classify_existing_pipelines, restore_existing_pipelines_as_sast),
        migrations.AddConstraint(
            model_name="aistpipeline",
            constraint=models.CheckConstraint(condition=models.Q(models.Q(("execution_type", "SAST"), ("project_version__isnull", False), ("trigger_project_version__isnull", True)), models.Q(("execution_type", "DAST"), ("trigger_project_version__isnull", False)), models.Q(("execution_type", "MANUAL_IMPORT"), ("trigger_project_version__isnull", True)), _connector="OR"), name="aist_pipeline_execution_source_valid"),
        ),
        migrations.CreateModel(
            name="DastIntegrationState",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("validation_state", models.CharField(choices=[("UNVALIDATED", "Unvalidated"), ("VALIDATING", "Validating"), ("READY", "Ready"), ("INVALID", "Invalid")], default="UNVALIDATED", max_length=16)),
                ("validated_at", models.DateTimeField(blank=True, null=True)),
                ("validation_error_code", models.CharField(blank=True, default="", max_length=64)),
                ("contract_version", models.CharField(blank=True, default="", max_length=32)),
                ("capabilities_etag", models.CharField(blank=True, default="", max_length=255)),
                ("capabilities_synced_at", models.DateTimeField(blank=True, null=True)),
                ("sync_error_code", models.CharField(blank=True, default="", max_length=64)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddConstraint(
            model_name="orgintegration",
            constraint=models.UniqueConstraint(condition=models.Q(("integration_type", "DAST"), ("is_active", True)), fields=("organization",), name="one_active_dast_integration_per_org"),
        ),
        migrations.AddField(
            model_name="dastintegrationstate",
            name="integration",
            field=models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="dast_state", to="aist.orgintegration"),
        ),
        migrations.CreateModel(
            name="DastOnboardingBundleUse",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("integrator_public_id", models.CharField(max_length=255, unique=True)),
                ("used_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("org_integration", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="onboarding_bundle_uses", to="aist.orgintegration")),
            ],
        ),
        migrations.CreateModel(
            name="DastTarget",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider_id", models.CharField(max_length=255)),
                ("display_name", models.CharField(max_length=255)),
                ("contract_revision", models.CharField(max_length=64)),
                ("capability_revision", models.CharField(max_length=96)),
                ("schema_digest", models.CharField(max_length=96)),
                ("parameter_schema", models.JSONField(default=dict)),
                ("provider_defaults", models.JSONField(default=dict)),
                ("repository_keys", models.JSONField(default=list)),
                ("autonomous_ready", models.BooleanField(default=False)),
                ("is_available", models.BooleanField(default=True)),
                ("last_seen_at", models.DateTimeField()),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("integration", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="dast_targets", to="aist.orgintegration")),
            ],
            options={
                "ordering": ["integration", "provider_id"],
            },
        ),
        migrations.CreateModel(
            name="DastProjectBinding",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_repo_key", models.CharField(max_length=128)),
                ("enabled", models.BooleanField(default=True)),
                ("parameter_snapshot", models.JSONField(default=dict)),
                ("autonomous_enabled", models.BooleanField(default=False)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="dast_bindings", to="aist.aistproject")),
                ("target", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="project_bindings", to="aist.dasttarget")),
            ],
            options={
                "ordering": ["project", "target"],
            },
        ),
        migrations.AddConstraint(
            model_name="dasttarget",
            constraint=models.UniqueConstraint(fields=("integration", "provider_id"), name="uniq_dast_target_provider_per_integration"),
        ),
        migrations.AddConstraint(
            model_name="dastprojectbinding",
            constraint=models.UniqueConstraint(fields=("project", "target"), name="uniq_dast_binding_project_target"),
        ),
        migrations.RunSQL(
            sql="\nCREATE OR REPLACE FUNCTION aist_validate_dast_target()\nRETURNS trigger AS $$\nDECLARE\n    integration_type_value text;\nBEGIN\n    IF TG_OP = 'UPDATE' AND OLD.integration_id IS DISTINCT FROM NEW.integration_id THEN\n        RAISE EXCEPTION 'A discovered DAST target cannot move to another integration'\n            USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_target_integration_immutable';\n    END IF;\n    SELECT integration_type INTO integration_type_value\n      FROM aist_orgintegration\n     WHERE id = NEW.integration_id\n     FOR SHARE;\n    IF integration_type_value IS DISTINCT FROM 'DAST' THEN\n        RAISE EXCEPTION 'DastTarget requires a DAST OrgIntegration'\n            USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_target_requires_dast_integration';\n    END IF;\n\n    IF TG_OP = 'UPDATE' AND OLD.repository_keys IS DISTINCT FROM NEW.repository_keys AND EXISTS (\n        SELECT 1\n          FROM aist_dastprojectbinding AS binding\n         WHERE binding.target_id = NEW.id\n           AND NOT (NEW.repository_keys ? binding.source_repo_key)\n    ) THEN\n        RAISE EXCEPTION 'DastTarget repository keys cannot invalidate existing bindings'\n            USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_target_repository_keys_protected';\n    END IF;\n    RETURN NEW;\nEND;\n$$ LANGUAGE plpgsql;\n\nCREATE TRIGGER aist_dast_target_invariants\nBEFORE INSERT OR UPDATE OF integration_id, repository_keys ON aist_dasttarget\nFOR EACH ROW EXECUTE FUNCTION aist_validate_dast_target();\n\nCREATE OR REPLACE FUNCTION aist_validate_dast_project_binding()\nRETURNS trigger AS $$\nDECLARE\n    target_row record;\n    project_organization_id integer;\nBEGIN\n    SELECT target.repository_keys, integration.organization_id,\n           integration.integration_type, integration.is_active\n      INTO target_row\n      FROM aist_dasttarget AS target\n      JOIN aist_orgintegration AS integration ON integration.id = target.integration_id\n     WHERE target.id = NEW.target_id\n     FOR SHARE OF target, integration;\n\n    SELECT organization.id\n      INTO project_organization_id\n      FROM aist_aistproject AS project\n      JOIN dojo_product AS product ON product.id = project.product_id\n      JOIN aist_organization AS organization ON organization.product_type_id = product.prod_type_id\n     WHERE project.id = NEW.project_id\n     FOR SHARE OF project, product, organization;\n\n    IF target_row.integration_type IS DISTINCT FROM 'DAST'\n       OR target_row.is_active IS DISTINCT FROM TRUE THEN\n        RAISE EXCEPTION 'DastProjectBinding requires the active DAST integration'\n            USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_binding_active_integration';\n    END IF;\n    IF project_organization_id IS NULL\n       OR project_organization_id IS DISTINCT FROM target_row.organization_id THEN\n        RAISE EXCEPTION 'DastProjectBinding cannot cross an organization boundary'\n            USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_binding_tenant_match';\n    END IF;\n    IF NOT (target_row.repository_keys ? NEW.source_repo_key) THEN\n        RAISE EXCEPTION 'DastProjectBinding source repository key is not advertised'\n            USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_binding_repository_key';\n    END IF;\n    RETURN NEW;\nEND;\n$$ LANGUAGE plpgsql;\n\nCREATE TRIGGER aist_dast_project_binding_invariants\nBEFORE INSERT OR UPDATE OF project_id, target_id, source_repo_key ON aist_dastprojectbinding\nFOR EACH ROW EXECUTE FUNCTION aist_validate_dast_project_binding();\n",
            reverse_sql="\nDROP TRIGGER IF EXISTS aist_dast_project_binding_invariants ON aist_dastprojectbinding;\nDROP FUNCTION IF EXISTS aist_validate_dast_project_binding();\nDROP TRIGGER IF EXISTS aist_dast_target_invariants ON aist_dasttarget;\nDROP FUNCTION IF EXISTS aist_validate_dast_target();\n",
        ),
        migrations.RenameModel(
            old_name="PipelineLaunchQueue",
            new_name="PipelineLaunchRequest",
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="updated",
            field=models.DateTimeField(auto_now=True),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="origin",
            field=models.CharField(choices=[("MANUAL", "Manual"), ("SCHEDULE", "Schedule"), ("SCM_WEBHOOK", "SCM webhook"), ("RECONCILER", "Reconciler")], default="SCHEDULE", max_length=24),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="execution_type",
            field=models.CharField(choices=[("SAST", "SAST"), ("DAST", "DAST"), ("MANUAL_IMPORT", "Manual report import")], db_index=True, default="SAST", max_length=24),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="dast_binding",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="launch_requests", to="aist.dastprojectbinding"),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="trigger_project_version",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="launch_requests", to="aist.aistprojectversion"),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="requester",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="pipeline_launch_requests", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="api_token",
            field=models.ForeignKey(blank=True, help_text="Public PAT record used for authority revalidation; never stores the token secret.", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="pipeline_launch_requests", to="aist.aistapitoken"),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="authority_kind",
            field=models.CharField(choices=[("USER", "User session"), ("PAT", "AIST personal access token"), ("SCHEDULE", "Stored schedule authority"), ("SCM_WEBHOOK", "Verified SCM webhook"), ("RECONCILER", "Existing request reconciliation")], default="SCHEDULE", max_length=24),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="params_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="capability_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="state",
            field=models.CharField(choices=[("PENDING", "Pending"), ("CLAIMED", "Claimed"), ("PLANNED", "Planned"), ("PUBLISHED", "Published"), ("DISPATCHED", "Dispatched"), ("SUPERSEDED", "Superseded"), ("FAILED", "Failed"), ("EXPIRED", "Expired"), ("CANCELLED", "Cancelled")], db_index=True, default="PENDING", max_length=24),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="coalesce_key",
            field=models.CharField(blank=True, db_index=True, max_length=128, null=True),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="priority",
            field=models.SmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="not_before",
            field=models.DateTimeField(db_index=True, default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="claim_owner",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="claimed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="task_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="failure_code",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="failure_detail",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.RunPython(backfill_launch_requests, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="pipelinelaunchrequest",
            name="dispatched",
        ),
        migrations.AlterField(
            model_name="pipelinelaunchrequest",
            name="task_id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="pipelinelaunchrequest",
            name="launch_config",
            field=models.ForeignKey(blank=True, help_text="Launch config used to build pipeline_args snapshot.", null=True, on_delete=django.db.models.deletion.CASCADE, related_name="launch_requests", to="aist.aistprojectlaunchconfig"),
        ),
        migrations.AlterField(
            model_name="pipelinelaunchrequest",
            name="pipeline",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="launch_request", to="aist.aistpipeline"),
        ),
        migrations.AddIndex(
            model_name="pipelinelaunchrequest",
            index=models.Index(fields=["state", "not_before", "priority"], name="aist_launch_req_dispatch_idx"),
        ),
        migrations.RunSQL(
            sql="\nCREATE OR REPLACE FUNCTION aist_protect_launch_request_snapshots()\nRETURNS trigger AS $$\nBEGIN\n    IF OLD.params_snapshot IS DISTINCT FROM NEW.params_snapshot\n       OR OLD.capability_snapshot IS DISTINCT FROM NEW.capability_snapshot THEN\n        RAISE EXCEPTION 'Pipeline launch request snapshots are immutable'\n            USING ERRCODE = '23514', CONSTRAINT = 'aist_launch_request_snapshots_immutable';\n    END IF;\n    RETURN NEW;\nEND;\n$$ LANGUAGE plpgsql;\n\nCREATE TRIGGER aist_launch_request_snapshots_immutable\nBEFORE UPDATE OF params_snapshot, capability_snapshot ON aist_pipelinelaunchrequest\nFOR EACH ROW EXECUTE FUNCTION aist_protect_launch_request_snapshots();\n",
            reverse_sql="\nDROP TRIGGER IF EXISTS aist_launch_request_snapshots_immutable ON aist_pipelinelaunchrequest;\nDROP FUNCTION IF EXISTS aist_protect_launch_request_snapshots();\n",
        ),
        migrations.CreateModel(
            name="PipelineExecutionLease",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("resource_key", models.CharField(max_length=255)),
                ("slot", models.PositiveSmallIntegerField()),
                ("acquired_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("heartbeat_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("released_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("pipeline", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="execution_leases", to="aist.aistpipeline")),
                ("request", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="execution_leases", to="aist.pipelinelaunchrequest")),
            ],
            options={
                "ordering": ["resource_key", "slot", "acquired_at"],
                "constraints": [models.UniqueConstraint(condition=models.Q(("released_at__isnull", True)), fields=("resource_key", "slot"), name="uniq_active_execution_lease_slot")],
            },
        ),
        migrations.AddField(
            model_name="aistprojectlaunchconfig",
            name="execution_type",
            field=models.CharField(choices=[("SAST", "SAST"), ("DAST", "DAST")], db_index=True, default="SAST", max_length=24),
        ),
        migrations.AddField(
            model_name="aistprojectlaunchconfig",
            name="dast_binding",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="launch_configs", to="aist.dastprojectbinding"),
        ),
        migrations.AddConstraint(
            model_name="aistprojectlaunchconfig",
            constraint=models.CheckConstraint(condition=models.Q(models.Q(("dast_binding__isnull", True), ("execution_type", "SAST")), models.Q(("dast_binding__isnull", False), ("execution_type", "DAST")), _connector="OR"), name="aist_launch_config_execution_target_valid"),
        ),
        migrations.AddConstraint(
            model_name="aistprojectlaunchconfig",
            constraint=models.CheckConstraint(condition=models.Q(("execution_type", "SAST"), models.Q(("params__has_key", "analyzers"), _negated=True), _connector="OR"), name="aist_dast_launch_config_no_analyzers"),
        ),
        migrations.RunSQL(
            sql="\nCREATE OR REPLACE FUNCTION aist_validate_launch_config_target()\nRETURNS trigger AS $$\nDECLARE\n    binding_row record;\nBEGIN\n    IF NEW.execution_type = 'DAST' THEN\n        SELECT project_id, enabled INTO binding_row\n          FROM aist_dastprojectbinding\n         WHERE id = NEW.dast_binding_id\n         FOR SHARE;\n        IF binding_row.project_id IS NULL OR binding_row.project_id IS DISTINCT FROM NEW.project_id THEN\n            RAISE EXCEPTION 'DAST launch config binding must belong to the same project'\n                USING ERRCODE = '23514', CONSTRAINT = 'aist_launch_config_binding_project_match';\n        END IF;\n        IF binding_row.enabled IS DISTINCT FROM TRUE THEN\n            RAISE EXCEPTION 'DAST launch config binding must be enabled'\n                USING ERRCODE = '23514', CONSTRAINT = 'aist_launch_config_binding_enabled';\n        END IF;\n    END IF;\n    RETURN NEW;\nEND;\n$$ LANGUAGE plpgsql;\n\nCREATE TRIGGER aist_launch_config_target_invariants\nBEFORE INSERT OR UPDATE OF execution_type, project_id, dast_binding_id\nON aist_aistprojectlaunchconfig\nFOR EACH ROW EXECUTE FUNCTION aist_validate_launch_config_target();\n",
            reverse_sql="\nDROP TRIGGER IF EXISTS aist_launch_config_target_invariants ON aist_aistprojectlaunchconfig;\nDROP FUNCTION IF EXISTS aist_validate_launch_config_target();\n",
        ),
        migrations.AlterField(
            model_name="dastintegrationstate",
            name="validation_state",
            field=models.CharField(choices=[("UNVALIDATED", "Unvalidated"), ("PENDING_VALIDATION", "Pending validation"), ("VALIDATING", "Validating"), ("READY", "Ready"), ("INVALID", "Invalid")], default="UNVALIDATED", max_length=24),
        ),
        migrations.AddField(
            model_name="dastintegrationstate",
            name="validation_claimed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dastintegrationstate",
            name="validation_generation",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="dastintegrationstate",
            name="validation_task_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="dastintegrationstate",
            name="sync_claimed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dastintegrationstate",
            name="sync_generation",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="dastintegrationstate",
            name="sync_task_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="dastprojectbinding",
            name="parameter_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="client_request_key_hash",
            field=models.CharField(blank=True, editable=False, help_text="Server-namespaced digest of an optional producer idempotency key.", max_length=64, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="superseded_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="superseded_requests", to="aist.pipelinelaunchrequest"),
        ),
        migrations.AddConstraint(
            model_name="pipelinelaunchrequest",
            constraint=models.CheckConstraint(condition=models.Q(models.Q(("state", "SUPERSEDED"), ("superseded_by__isnull", False)), models.Q(models.Q(("state", "SUPERSEDED"), _negated=True), ("superseded_by__isnull", True)), _connector="OR"), name="aist_launch_request_supersede_link_valid"),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="task_args_snapshot",
            field=models.JSONField(blank=True, default=list, editable=False, help_text="Secret-free JSON task arguments frozen before broker publication."),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="task_name",
            field=models.CharField(blank=True, default="", editable=False, help_text="Trusted Celery task selected by the launch adapter during planning.", max_length=128),
        ),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="capacity_retry_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(backfill_pending_request_expiry, migrations.RunPython.noop),
        migrations.AddField(
            model_name="pipelinelaunchrequest",
            name="initial_launch_data_snapshot",
            field=models.JSONField(blank=True, default=dict, editable=False, help_text="Secret-free pipeline launch metadata frozen by the authorized producer."),
        ),
        migrations.AddField(
            model_name="aistpipeline",
            name="external_cancel_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="aistpipeline",
            name="external_execution_deadline",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="aistpipeline",
            name="external_execution_outcome",
            field=models.CharField(blank=True, choices=[("RUNNING", "Running"), ("STOP_PENDING", "Remote stop pending"), ("TERMINAL", "Provider terminal"), ("CANCELLED_BEFORE_START", "Cancelled before provider start"), ("UNREACHABLE", "Provider unreachable")], default="", max_length=32),
        ),
        migrations.AddConstraint(
            model_name="aistpipeline",
            constraint=models.CheckConstraint(condition=models.Q(("execution_type", "DAST"), models.Q(("external_cancel_requested_at__isnull", True), ("external_execution_deadline__isnull", True), ("external_execution_outcome", ""), ("external_log_cursor", 0), ("external_run_id__isnull", True)), _connector="OR"), name="aist_pipeline_dast_control_fields_valid"),
        ),
    ]
