from __future__ import annotations

import hashlib

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _create_shared_default_script(apps, schema_editor):
    """
    Data migration: create the shared singleton AISTProjectScript and set it
    as active_script for every existing AISTProject.

    Uses a single shared instance (is_shared=True, project=None) so future
    updates to the default script propagate everywhere automatically.
    """
    from aist.default_script import DEFAULT_ENTRYPOINT_SCRIPT  # noqa: PLC0415

    AISTProject = apps.get_model("aist", "AISTProject")
    AISTProjectScript = apps.get_model("aist", "AISTProjectScript")

    sha256 = hashlib.sha256(DEFAULT_ENTRYPOINT_SCRIPT.encode()).hexdigest()

    # Historical model does not call custom save(), so set sha256 manually.
    shared = AISTProjectScript.objects.create(
        project=None,
        is_shared=True,
        content=DEFAULT_ENTRYPOINT_SCRIPT,
        sha256=sha256,
    )

    for project in AISTProject.objects.all():
        project.active_script = shared
        project.save(update_fields=["active_script"])


def _remove_shared_default_script(apps, schema_editor):
    """Reverse: detach and delete the shared default script."""
    AISTProject = apps.get_model("aist", "AISTProject")
    AISTProjectScript = apps.get_model("aist", "AISTProjectScript")

    AISTProject.objects.update(active_script=None)
    AISTProjectScript.objects.filter(is_shared=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0018_aistfindingannotation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # 1. Create AISTProjectScript with nullable project and is_shared flag.
        migrations.CreateModel(
            name="AISTProjectScript",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("content", models.TextField()),
                ("sha256", models.CharField(editable=False, max_length=64)),
                ("is_shared", models.BooleanField(
                    default=False,
                    help_text="True for the singleton shared default script (project=None).",
                )),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "project",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="script_revisions",
                        to="aist.aistproject",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        # 2. Add active_script FK to AISTProject (nullable) BEFORE data migration.
        migrations.AddField(
            model_name="aistproject",
            name="active_script",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="aist.aistprojectscript",
            ),
        ),
        # 3. Create the shared singleton and link every existing project to it.
        migrations.RunPython(
            _create_shared_default_script,
            reverse_code=_remove_shared_default_script,
        ),
        # 4. Drop the old file-path field now that content lives in the DB.
        migrations.RemoveField(
            model_name="aistproject",
            name="script_path",
        ),
        # 5. Partial unique index: enforce that only one is_shared=True row can
        #    ever exist, making get_shared_default() race-condition-safe.
        migrations.AddConstraint(
            model_name="aistprojectscript",
            constraint=models.UniqueConstraint(
                fields=["is_shared"],
                condition=models.Q(is_shared=True),
                name="uniq_aistprojectscript_shared_singleton",
            ),
        ),
    ]
