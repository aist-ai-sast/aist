from django.db import migrations

from aist.profile import ProjectProfile
from aist.utils.pipeline_imports import _import_sast_pipeline_package

_import_sast_pipeline_package()

from pipeline.config_utils import TimeClass  # noqa: E402

# Stored launch settings an earlier release accepted but launches now validate at admission.
# Each is rewritten to the value it already behaved as, so nothing that ran before changes.


def admit_stored_settings(apps, schema_editor):
    project_model = apps.get_model("aist", "AISTProject")
    for project in project_model.objects.exclude(profile=None).only("pk", "profile").iterator():
        try:
            ProjectProfile.validate_dict(project.profile)
        except ValueError:
            project_model.objects.filter(pk=project.pk).update(
                profile=ProjectProfile.from_dict(project.profile).as_dict(),
            )

    launch_config_model = apps.get_model("aist", "AISTProjectLaunchConfig")
    for config in launch_config_model.objects.only("pk", "params").iterator():
        params = config.params
        if not isinstance(params, dict) or params.get("time_class_level") is None:
            continue
        time_class = TimeClass.of(params["time_class_level"])
        if time_class != params["time_class_level"]:
            launch_config_model.objects.filter(pk=config.pk).update(
                params={**params, "time_class_level": time_class.value},
            )


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0058_clear_vendor_marketing_announcement"),
    ]

    operations = [
        # The rewritten values behave as the old ones did; there is nothing to restore.
        migrations.RunPython(admit_stored_settings, reverse_code=migrations.RunPython.noop),
    ]
