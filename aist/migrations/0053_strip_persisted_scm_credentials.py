"""
Remove the system-computed launch env from already persisted SAST launch params.

`REPO_URL` embeds the SCM integration credential (GitLab PAT, Gerrit HTTP password, GitHub
installation token), so it must never sit at rest in a launch config or a frozen launch
request snapshot. From this release on it is injected per run by
`SastPipelineArguments.runtime_env`; this migration scrubs the rows written before that
split, together with the `PROJECT_NAME` value that is derived the same way.

`PipelineLaunchRequest.coalesce_key` is deliberately left untouched. It is a frozen
execution identity and recomputing it here would need the enqueue-time schedule
resolution. The stored key of an already-finished request is never recomputed, and for a
request still PENDING at deploy time the only effect is that it does not coalesce against
requests enqueued after the deploy (and that replaying its Idempotency-Key reports a
conflict) until it expires.
"""

from django.db import migrations

BATCH_SIZE = 500
SYSTEM_ENV_KEYS = ("REPO_URL", "PROJECT_NAME")

_DISABLE_SNAPSHOT_TRIGGER = (
    "ALTER TABLE aist_pipelinelaunchrequest DISABLE TRIGGER aist_launch_request_snapshots_immutable;"
)
_ENABLE_SNAPSHOT_TRIGGER = (
    "ALTER TABLE aist_pipelinelaunchrequest ENABLE TRIGGER aist_launch_request_snapshots_immutable;"
)


def _stripped(params):
    """Return params without the system env keys, or None when there is nothing to strip."""
    if not isinstance(params, dict):
        return None
    env = params.get("env")
    if not isinstance(env, dict) or not any(key in env for key in SYSTEM_ENV_KEYS):
        return None
    cleaned = dict(params)
    cleaned["env"] = {key: value for key, value in env.items() if key not in SYSTEM_ENV_KEYS}
    return cleaned


def _scrub(queryset, field):
    """Rewrite `field` in batches, skipping rows that already carry no system env."""
    model = queryset.model
    pending = []
    for row in queryset.order_by("pk").iterator(chunk_size=BATCH_SIZE):
        cleaned = _stripped(getattr(row, field))
        if cleaned is None:
            continue
        setattr(row, field, cleaned)
        pending.append(row)
        if len(pending) >= BATCH_SIZE:
            model.objects.bulk_update(pending, [field])
            pending = []
    if pending:
        model.objects.bulk_update(pending, [field])


def strip_persisted_scm_credentials(apps, schema_editor):
    launch_config_model = apps.get_model("aist", "AISTProjectLaunchConfig")
    launch_request_model = apps.get_model("aist", "PipelineLaunchRequest")

    # DAST params are a DastBindingParameters snapshot and never carried an env block.
    _scrub(launch_config_model.objects.filter(execution_type="SAST").only("pk", "params"), "params")

    # params_snapshot is immutable by DB trigger; this one-shot scrub is the only writer
    # allowed past it. DISABLE TRIGGER is transactional DDL inside this atomic migration,
    # so a failed rewrite rolls the trigger back on its own — re-enabling it from a
    # `finally` would only raise InFailedSqlTransaction and hide the real error.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(_DISABLE_SNAPSHOT_TRIGGER)
    _scrub(
        launch_request_model.objects.filter(execution_type="SAST").only("pk", "params_snapshot"),
        "params_snapshot",
    )
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(_ENABLE_SNAPSHOT_TRIGGER)


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0052_dastintegrationstate_sync_status"),
    ]

    operations = [
        # The reverse is a no-op: a removed credential is not something to put back.
        migrations.RunPython(strip_persisted_scm_credentials, migrations.RunPython.noop),
    ]
