"""
Version-level script binding: each AISTProjectVersion gets its own script FK.

Operations (in order):
1. Add AISTProjectVersion.script FK (nullable) — schema change
2. Backfill script=NULL versions with a project-scoped copy of the shared default
3. Remove AISTProject.active_script FK (replaced by @property)
"""
import hashlib

import django.db.models.deletion
from django.db import migrations, models


def _backfill_version_scripts(apps, schema_editor):
    AISTProjectScript = apps.get_model("aist", "AISTProjectScript")
    AISTProjectVersion = apps.get_model("aist", "AISTProjectVersion")

    shared = AISTProjectScript.objects.filter(is_shared=True).first()
    if shared is None:
        # No shared default exists yet — nothing to backfill.
        return

    versions_without_script = AISTProjectVersion.objects.filter(script__isnull=True).select_related("project")
    if not versions_without_script.exists():
        return

    # Cache project_id → project-scoped script to avoid redundant creates.
    project_script_cache: dict = {}

    for version in versions_without_script.iterator():
        project_id = version.project_id
        if project_id not in project_script_cache:
            sha = hashlib.sha256(shared.content.encode()).hexdigest()
            existing = AISTProjectScript.objects.filter(
                sha256=sha, project_id=project_id, is_shared=False,
            ).first()
            if existing:
                project_script_cache[project_id] = existing
            else:
                new_script = AISTProjectScript.objects.create(
                    content=shared.content,
                    project_id=project_id,
                    is_shared=False,
                    created_by=None,
                )
                project_script_cache[project_id] = new_script

        version.script = project_script_cache[project_id]
        version.save(update_fields=["script"])


def _reverse_backfill(apps, schema_editor):
    # Reversing the data migration is a no-op: NULL script versions would need
    # re-identification, which is not safely reversible without additional metadata.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("aist", "0025_aistaifindingresponse_fix"),
    ]

    operations = [
        # 1. Add version.script FK.
        migrations.AddField(
            model_name="aistprojectversion",
            name="script",
            field=models.ForeignKey(
                blank=True,
                help_text="Script used for this version. Always project-scoped (script.project == self.project).",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="version_overrides",
                to="aist.aistprojectscript",
            ),
        ),
        # 2. Backfill existing versions.
        migrations.RunPython(_backfill_version_scripts, _reverse_backfill),
        # 3. Remove the now-superseded project-level FK (replaced by @property).
        migrations.RemoveField(
            model_name="aistproject",
            name="active_script",
        ),
    ]
