from django.db import migrations, models


def backfill_launch_requirements(apps, schema_editor):
    del schema_editor
    target_model = apps.get_model("aist", "DastTarget")
    # Every DastTarget row that existed before this migration was persisted under the OLD
    # invariant, which forced repository_keys to be non-empty for every target regardless of
    # its real scenario. So "requires a repository trigger" is the only backward-correct
    # classification until the next catalog sync repopulates this from the provider's real,
    # per-target answer -- defaulting to [] instead would silently reclassify every existing
    # source-bound target as sourceless and break its already-configured trigger version.
    target_model.objects.update(launch_requirements=["repository-trigger"])


def reverse_backfill(apps, schema_editor):
    del schema_editor
    # The column itself is removed by AddField's reverse; nothing to unwind here.


CREATE_RELATION_TRIGGERS = """
DROP TRIGGER IF EXISTS aist_launch_config_source_invariants ON aist_aistprojectlaunchconfig;
DROP TRIGGER IF EXISTS aist_launch_request_source_invariants ON aist_pipelinelaunchrequest;
DROP TRIGGER IF EXISTS aist_dast_target_invariants ON aist_dasttarget;
DROP TRIGGER IF EXISTS aist_dast_project_binding_invariants ON aist_dastprojectbinding;

CREATE OR REPLACE FUNCTION aist_validate_dast_target()
RETURNS trigger AS $$
DECLARE
    integration_type_value text;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.integration_id IS DISTINCT FROM NEW.integration_id THEN
        RAISE EXCEPTION 'A discovered DAST target cannot move to another integration'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_target_integration_immutable';
    END IF;
    SELECT integration_type INTO integration_type_value
      FROM aist_orgintegration
     WHERE id = NEW.integration_id
     FOR SHARE;
    IF integration_type_value IS DISTINCT FROM 'DAST' THEN
        RAISE EXCEPTION 'DastTarget requires a DAST OrgIntegration'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_target_requires_dast_integration';
    END IF;

    -- A target's repository_keys or launch_requirements can each independently strand an
    -- existing binding: shrinking repository_keys can drop a still-required key, and flipping
    -- launch_requirements can make an existing source_repo_key newly required or newly forbidden.
    IF TG_OP = 'UPDATE' AND (
           OLD.repository_keys IS DISTINCT FROM NEW.repository_keys
        OR OLD.launch_requirements IS DISTINCT FROM NEW.launch_requirements
       ) AND EXISTS (
        SELECT 1
          FROM aist_dastprojectbinding AS binding
         WHERE binding.target_id = NEW.id
           AND (
                (
                    (NEW.launch_requirements @> '["repository-trigger"]'::jsonb)
                    AND NOT (NEW.repository_keys ? binding.source_repo_key)
                )
                OR (
                    NOT (NEW.launch_requirements @> '["repository-trigger"]'::jsonb)
                    AND binding.source_repo_key <> ''
                )
           )
    ) THEN
        RAISE EXCEPTION 'DastTarget repository keys cannot invalidate existing bindings'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_target_repository_keys_protected';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER aist_dast_target_invariants
BEFORE INSERT OR UPDATE OF integration_id, repository_keys, launch_requirements ON aist_dasttarget
FOR EACH ROW EXECUTE FUNCTION aist_validate_dast_target();

CREATE OR REPLACE FUNCTION aist_validate_dast_project_binding()
RETURNS trigger AS $$
DECLARE
    target_row record;
    project_organization_id integer;
BEGIN
    SELECT target.repository_keys, target.launch_requirements, integration.organization_id,
           integration.integration_type, integration.is_active
      INTO target_row
      FROM aist_dasttarget AS target
      JOIN aist_orgintegration AS integration ON integration.id = target.integration_id
     WHERE target.id = NEW.target_id
     FOR SHARE OF target, integration;

    SELECT organization.id
      INTO project_organization_id
      FROM aist_aistproject AS project
      JOIN dojo_product AS product ON product.id = project.product_id
      JOIN aist_organization AS organization ON organization.product_type_id = product.prod_type_id
     WHERE project.id = NEW.project_id
     FOR SHARE OF project, product, organization;

    IF target_row.integration_type IS DISTINCT FROM 'DAST'
       OR target_row.is_active IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'DastProjectBinding requires the active DAST integration'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_binding_active_integration';
    END IF;
    IF project_organization_id IS NULL
       OR project_organization_id IS DISTINCT FROM target_row.organization_id THEN
        RAISE EXCEPTION 'DastProjectBinding cannot cross an organization boundary'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_binding_tenant_match';
    END IF;
    IF (target_row.launch_requirements @> '["repository-trigger"]'::jsonb) THEN
        IF NOT (target_row.repository_keys ? NEW.source_repo_key) THEN
            RAISE EXCEPTION 'DastProjectBinding source repository key is not advertised'
                USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_binding_repository_key';
        END IF;
    ELSIF NEW.source_repo_key <> '' THEN
        RAISE EXCEPTION 'DastProjectBinding for a sourceless target cannot select a source repository'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_binding_repository_key_forbidden';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER aist_dast_project_binding_invariants
BEFORE INSERT OR UPDATE OF project_id, target_id, source_repo_key ON aist_dastprojectbinding
FOR EACH ROW EXECUTE FUNCTION aist_validate_dast_project_binding();

CREATE OR REPLACE FUNCTION aist_validate_launch_config_source()
RETURNS trigger AS $$
DECLARE
    version_row record;
    requires_repo boolean;
BEGIN
    IF NEW.execution_type = 'DAST' THEN
        SELECT (t.launch_requirements @> '["repository-trigger"]'::jsonb) INTO requires_repo
          FROM aist_dastprojectbinding b
          JOIN aist_dasttarget t ON t.id = b.target_id
         WHERE b.id = NEW.dast_binding_id
         FOR SHARE OF t;
        IF requires_repo THEN
            IF NEW.trigger_project_version_id IS NULL THEN
                RAISE EXCEPTION 'DAST launch config requires a Git trigger version'
                    USING ERRCODE = '23514', CONSTRAINT = 'aist_launch_config_trigger_required';
            END IF;
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
        ELSIF NEW.trigger_project_version_id IS NOT NULL THEN
            RAISE EXCEPTION 'DAST launch config for a sourceless binding cannot select a trigger version'
                USING ERRCODE = '23514', CONSTRAINT = 'aist_launch_config_trigger_forbidden';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER aist_launch_config_source_invariants
BEFORE INSERT OR UPDATE OF execution_type, project_id, dast_binding_id, trigger_project_version_id
ON aist_aistprojectlaunchconfig
FOR EACH ROW EXECUTE FUNCTION aist_validate_launch_config_source();

CREATE OR REPLACE FUNCTION aist_validate_launch_request_source()
RETURNS trigger AS $$
DECLARE
    binding_row record;
    version_row record;
    requires_repo boolean;
BEGIN
    IF NEW.execution_type = 'DAST' THEN
        SELECT project_id INTO binding_row
          FROM aist_dastprojectbinding
         WHERE id = NEW.dast_binding_id
         FOR SHARE;
        SELECT (t.launch_requirements @> '["repository-trigger"]'::jsonb) INTO requires_repo
          FROM aist_dastprojectbinding b
          JOIN aist_dasttarget t ON t.id = b.target_id
         WHERE b.id = NEW.dast_binding_id
         FOR SHARE OF t;
        IF binding_row.project_id IS NULL OR binding_row.project_id IS DISTINCT FROM NEW.project_id THEN
            RAISE EXCEPTION 'DAST launch request binding must belong to the same project'
                USING ERRCODE = '23514', CONSTRAINT = 'aist_launch_request_binding_project_match';
        END IF;
        IF requires_repo THEN
            IF NEW.trigger_project_version_id IS NULL THEN
                RAISE EXCEPTION 'DAST launch request requires a Git trigger version'
                    USING ERRCODE = '23514', CONSTRAINT = 'aist_launch_request_trigger_required';
            END IF;
            SELECT project_id, version_type INTO version_row
              FROM aist_aistprojectversion
             WHERE id = NEW.trigger_project_version_id
             FOR SHARE;
            IF version_row.project_id IS NULL OR version_row.project_id IS DISTINCT FROM NEW.project_id THEN
                RAISE EXCEPTION 'DAST launch request trigger must belong to the same project'
                    USING ERRCODE = '23514', CONSTRAINT = 'aist_launch_request_trigger_project_match';
            END IF;
            IF version_row.version_type NOT IN ('GIT_BRANCH', 'GIT_HASH') THEN
                RAISE EXCEPTION 'DAST launch request trigger must be a Git version'
                    USING ERRCODE = '23514', CONSTRAINT = 'aist_launch_request_trigger_git';
            END IF;
        ELSIF NEW.trigger_project_version_id IS NOT NULL THEN
            RAISE EXCEPTION 'DAST launch request for a sourceless binding cannot select a trigger version'
                USING ERRCODE = '23514', CONSTRAINT = 'aist_launch_request_trigger_forbidden';
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

RESTORE_PREVIOUS_RELATION_TRIGGERS = """
DROP TRIGGER IF EXISTS aist_launch_config_source_invariants ON aist_aistprojectlaunchconfig;
DROP TRIGGER IF EXISTS aist_launch_request_source_invariants ON aist_pipelinelaunchrequest;
DROP TRIGGER IF EXISTS aist_dast_target_invariants ON aist_dasttarget;
DROP TRIGGER IF EXISTS aist_dast_project_binding_invariants ON aist_dastprojectbinding;

CREATE OR REPLACE FUNCTION aist_validate_dast_target()
RETURNS trigger AS $$
DECLARE
    integration_type_value text;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.integration_id IS DISTINCT FROM NEW.integration_id THEN
        RAISE EXCEPTION 'A discovered DAST target cannot move to another integration'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_target_integration_immutable';
    END IF;
    SELECT integration_type INTO integration_type_value
      FROM aist_orgintegration
     WHERE id = NEW.integration_id
     FOR SHARE;
    IF integration_type_value IS DISTINCT FROM 'DAST' THEN
        RAISE EXCEPTION 'DastTarget requires a DAST OrgIntegration'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_target_requires_dast_integration';
    END IF;

    IF TG_OP = 'UPDATE' AND OLD.repository_keys IS DISTINCT FROM NEW.repository_keys AND EXISTS (
        SELECT 1
          FROM aist_dastprojectbinding AS binding
         WHERE binding.target_id = NEW.id
           AND NOT (NEW.repository_keys ? binding.source_repo_key)
    ) THEN
        RAISE EXCEPTION 'DastTarget repository keys cannot invalidate existing bindings'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_target_repository_keys_protected';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER aist_dast_target_invariants
BEFORE INSERT OR UPDATE OF integration_id, repository_keys ON aist_dasttarget
FOR EACH ROW EXECUTE FUNCTION aist_validate_dast_target();

CREATE OR REPLACE FUNCTION aist_validate_dast_project_binding()
RETURNS trigger AS $$
DECLARE
    target_row record;
    project_organization_id integer;
BEGIN
    SELECT target.repository_keys, integration.organization_id,
           integration.integration_type, integration.is_active
      INTO target_row
      FROM aist_dasttarget AS target
      JOIN aist_orgintegration AS integration ON integration.id = target.integration_id
     WHERE target.id = NEW.target_id
     FOR SHARE OF target, integration;

    SELECT organization.id
      INTO project_organization_id
      FROM aist_aistproject AS project
      JOIN dojo_product AS product ON product.id = project.product_id
      JOIN aist_organization AS organization ON organization.product_type_id = product.prod_type_id
     WHERE project.id = NEW.project_id
     FOR SHARE OF project, product, organization;

    IF target_row.integration_type IS DISTINCT FROM 'DAST'
       OR target_row.is_active IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'DastProjectBinding requires the active DAST integration'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_binding_active_integration';
    END IF;
    IF project_organization_id IS NULL
       OR project_organization_id IS DISTINCT FROM target_row.organization_id THEN
        RAISE EXCEPTION 'DastProjectBinding cannot cross an organization boundary'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_binding_tenant_match';
    END IF;
    IF NOT (target_row.repository_keys ? NEW.source_repo_key) THEN
        RAISE EXCEPTION 'DastProjectBinding source repository key is not advertised'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_dast_binding_repository_key';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER aist_dast_project_binding_invariants
BEFORE INSERT OR UPDATE OF project_id, target_id, source_repo_key ON aist_dastprojectbinding
FOR EACH ROW EXECUTE FUNCTION aist_validate_dast_project_binding();

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


class Migration(migrations.Migration):

    dependencies = [("aist", "0043_pipeline_lifecycle_states")]

    operations = [
        migrations.AddField(
            model_name="dasttarget",
            name="launch_requirements",
            field=models.JSONField(default=list, blank=True),
        ),
        # A target with no repository-trigger requirement legitimately has an empty
        # repository_keys list; it was never blank in practice before this migration, so
        # this state-only change (no DB effect) only now stops full_clean() from rejecting it.
        migrations.AlterField(
            model_name="dasttarget",
            name="repository_keys",
            field=models.JSONField(default=list, blank=True),
        ),
        migrations.RunPython(backfill_launch_requirements, reverse_backfill),
        migrations.AlterField(
            model_name="dastprojectbinding",
            name="source_repo_key",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        # Flush deferred trigger events before altering constraints on the same tables.
        migrations.RunSQL("SET CONSTRAINTS ALL IMMEDIATE", migrations.RunSQL.noop),
        migrations.RemoveConstraint(
            model_name="aistpipeline",
            name="aist_pipeline_execution_source_valid",
        ),
        migrations.AddConstraint(
            model_name="aistpipeline",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(execution_type="SAST", project_version__isnull=False, trigger_project_version__isnull=True)
                    | models.Q(execution_type="DAST")
                    | models.Q(execution_type="MANUAL_IMPORT", trigger_project_version__isnull=True)
                ),
                name="aist_pipeline_execution_source_valid",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="aistprojectlaunchconfig",
            name="aist_launch_config_execution_target_valid",
        ),
        migrations.AddConstraint(
            model_name="aistprojectlaunchconfig",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(execution_type="SAST", dast_binding__isnull=True, trigger_project_version__isnull=True)
                    | models.Q(execution_type="DAST", dast_binding__isnull=False)
                ),
                name="aist_launch_config_execution_target_valid",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="pipelinelaunchrequest",
            name="aist_launch_request_execution_target_valid",
        ),
        migrations.AddConstraint(
            model_name="pipelinelaunchrequest",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(execution_type="SAST", dast_binding__isnull=True, trigger_project_version__isnull=True)
                    | models.Q(execution_type="DAST", dast_binding__isnull=False)
                ),
                name="aist_launch_request_execution_target_valid",
            ),
        ),
        migrations.RunSQL(CREATE_RELATION_TRIGGERS, RESTORE_PREVIOUS_RELATION_TRIGGERS),
    ]
