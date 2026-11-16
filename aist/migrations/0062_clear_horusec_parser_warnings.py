"""Finish cleanly the SAST runs with no findings whose only recorded warning is the Horusec report-import TypeError."""
from django.db import migrations

HORUSEC_PARSER_FAILURE = {
    "source": "analyzer:horusec",
    "code": "import_failed",
    "message": "Report import failed: TypeError",
}


def clear_horusec_parser_warnings(apps, schema_editor):
    """
    Clear runs that found nothing, were not reconciled as failed, and recorded only that parser failure.

    A degradation that recorded no reason in the run's launch data is not visible to this selection.
    """
    alias = schema_editor.connection.alias
    pipelines = apps.get_model("aist", "AISTPipeline").objects.using(alias)
    findings = apps.get_model("dojo", "Finding").objects.using(alias)
    cleared = []
    for pipeline in pipelines.filter(status="FINISHED_WITH_WARNINGS", execution_type="SAST").iterator(chunk_size=200):
        launch_data = pipeline.launch_data if isinstance(pipeline.launch_data, dict) else {}
        reasons = launch_data.get("analyzer_degraded_reasons")
        if not reasons or any(reason != HORUSEC_PARSER_FAILURE for reason in reasons):
            continue
        if "execution_reconciliation" in launch_data or findings.filter(test__aist_pipelines=pipeline).exists():
            continue
        pipeline.launch_data = {**launch_data, "analyzer_degraded_reasons": []}
        pipeline.status = "FINISHED"
        cleared.append(pipeline)
    pipelines.bulk_update(cleared, ["status", "launch_data"], batch_size=200)


class Migration(migrations.Migration):
    dependencies = [("aist", "0061_relative_source_links")]
    operations = [
        # A finished run's earlier warning is not reconstructed; older code reads FINISHED as is.
        migrations.RunPython(clear_horusec_parser_warnings, reverse_code=migrations.RunPython.noop),
    ]
