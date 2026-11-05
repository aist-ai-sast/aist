from urllib.parse import urlsplit, urlunsplit

from django.db import migrations

from aist.link_builder import SOURCEFILE_LINK_META_NAME, LinkBuilder
from aist.utils.pipeline_imports import _import_sast_pipeline_package

_import_sast_pipeline_package()

from pipeline.defect_dojo.repo_info import checkout_relative_path  # noqa: E402

# A finding whose path is a `file:` URI gets the local path that URI names, relative to the trim_path its
# pipeline recorded; its source link is rebuilt when it names another path. A finding whose pipelines recorded
# no trim_path, or disagree on it, is left untouched. hash_code keeps the value computed at import.


def _trim_path_by_test(pipeline_tests, test_ids):
    """Each test's trim_path, for the tests in ``test_ids`` whose pipelines all recorded the same non-empty one."""
    recorded = {}
    for test_id, launch_data in pipeline_tests.objects.filter(test_id__in=test_ids).values_list(
        "test_id", "aistpipeline__launch_data",
    ):
        trim_path = launch_data.get("trim_path") if isinstance(launch_data, dict) else None
        recorded.setdefault(test_id, set()).add(trim_path or "")
    return {test_id: paths.pop() for test_id, paths in recorded.items() if len(paths) == 1 and "" not in paths}


def _rebuild_source_links(dojo_meta_model, file_path_by_finding):
    """Point each finding's source link, on the host it already names, at its ``file_path_by_finding`` path."""
    stale = []
    for meta in dojo_meta_model.objects.filter(
        name=SOURCEFILE_LINK_META_NAME, finding_id__in=file_path_by_finding,
    ).only("pk", "value", "finding_id"):
        linked = LinkBuilder.parse(meta.value)
        if linked is None:
            continue
        version_id, _subpath = linked
        rebuilt_path = urlsplit(LinkBuilder(version_id).build(file_path_by_finding[meta.finding_id])).path
        link = urlunsplit(urlsplit(meta.value)._replace(path=rebuilt_path))
        if link != meta.value:
            meta.value = link
            stale.append(meta)
    dojo_meta_model.objects.bulk_update(stale, ["value"], batch_size=500)


def relativize_file_uri_paths(apps, schema_editor):
    finding_model = apps.get_model("dojo", "Finding")
    findings = list(finding_model.objects.filter(file_path__istartswith="file:").only("pk", "file_path", "test_id"))
    if not findings:
        return

    trim_path_by_test = _trim_path_by_test(
        apps.get_model("aist", "AISTPipeline").tests.through, {finding.test_id for finding in findings},
    )
    changed = []
    for finding in findings:
        trim_path = trim_path_by_test.get(finding.test_id)
        if not trim_path:
            continue
        relative_path = checkout_relative_path(finding.file_path, trim_path)
        if relative_path != finding.file_path:
            finding.file_path = relative_path
            changed.append(finding)
    finding_model.objects.bulk_update(changed, ["file_path"], batch_size=500)
    _rebuild_source_links(apps.get_model("dojo", "DojoMeta"), {finding.pk: finding.file_path for finding in changed})


class Migration(migrations.Migration):

    dependencies = [
        ("aist", "0059_admissible_stored_launch_settings"),
    ]

    operations = [
        # The absolute URI named a path inside a removed scan workspace; there is nothing to restore.
        migrations.RunPython(relativize_file_uri_paths, reverse_code=migrations.RunPython.noop),
    ]
