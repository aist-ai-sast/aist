from django.urls import path
from django_github_app.views import AsyncWebhookView

from aist.views import (
    ai,
    dashboards,
    export,
    integrations,
    launch_configs,
    pipeline_logs,
    pipeline_progress,
    pipelines,
    projects,
)

app_name = "aist"
urlpatterns = [
    path("start/", pipelines.start_pipeline, name="start_pipeline"),
    path("pipelines/<str:pipeline_id>/", pipelines.pipeline_detail, name="pipeline_detail"),
    path("pipelines/<str:pipeline_id>/stop/", pipelines.stop_pipeline_view, name="pipeline_stop"),

    path("products/<int:product_id>/analyzers.json", ai.product_analyzers_json, name="product_analyzers_json"),
    path("findings/search.json", ai.search_findings_json, name="search_findings_json"),
    path("pipelines/<str:pipeline_id>/send_request_to_ai/", ai.send_request_to_ai, name="send_request_to_ai"),
    path("pipelines/<str:pipeline_id>/callback/", ai.pipeline_callback, name="pipeline_callback"),
    path("pipelines/<str:pipeline_id>/ai-response/<int:response_id>/delete/",
         ai.delete_ai_response,
         name="delete_ai_response"),

    path("pipelines/<str:pipeline_id>/delete/", pipelines.delete_pipeline_view, name="pipeline_delete"),
    path("pipelines/<str:pipeline_id>/logs/stream/", pipeline_logs.stream_logs_sse, name="pipeline_logs_stream"),
    path("pipelines/<str:pipeline_id>/logs/progressive/", pipeline_logs.pipeline_logs_progressive,
         name="pipeline_logs_progressive"),
    path("pipelines/<str:pipeline_id>/logs/", pipeline_logs.pipeline_logs_full, name="pipeline_logs_full"),
    path("pipelines/<str:pipeline_id>/logs/raw.txt", pipeline_logs.pipeline_logs_raw, name="pipeline_logs_raw"),
    path("pipelines/<str:pipeline_id>/logs/download/", pipeline_logs.pipeline_logs_download, name="pipeline_logs_download"),
    path("pipelines/<str:pipeline_id>/progress/deduplication", pipeline_progress.deduplication_progress_json,
         name="deduplication_progress"),
    path(
        "pipelines/<str:pipeline_id>/export-ai-results/",
        export.export_ai_results,
        name="export_ai_results",
    ),

    path("pipeline/<str:pipeline_id>/status/stream/", pipeline_progress.pipeline_status_stream, name="pipeline_status_stream"),
    path("aist/default-analyzers/", projects.default_analyzers, name="default_analyzers"),
    path("pipelines/", pipelines.pipeline_list, name="pipeline_list"),
    path("pipelines/<str:pipeline_id>/set_status_push_to_ai/", pipelines.pipeline_set_status, name="pipeline_set_status"),
    # TODO: make generic
    path("projects/<int:pk>/meta.json", projects.project_meta, name="project_meta"),
    path("pipeline/<str:pipeline_id>/progress/enrichment", pipeline_progress.pipeline_enrich_progress_sse,
         name="pipeline_enrich_progress"),
    path("projects/<int:project_id>/versions/create/", projects.project_version_create, name="project_version_create"),

    # AIST Projects UI
    path("projects/", projects.aist_project_list_view, name="aist_project_list"),
    path("projects/<int:project_id>/update/", projects.aist_project_update_view, name="aist_project_update"),

    # Github hooks
    path("github_hook/", AsyncWebhookView.as_view()),
    path(
        "projects/gitlab/list/",
        integrations.gitlab_projects_list,
        name="gitlab_projects_list",
    ),

    path("ai/filter/reference/", ai.ai_filter_reference, name="ai_filter_reference"),
    path("ai/filter/help/", ai.ai_filter_help, name="ai_filter_help"),
    path("ai/filter/validate/", ai.ai_filter_validate, name="ai_filter_validate"),
    path("launching/", dashboards.launching_dashboard, name="launching_dashboard"),
    path(
        "projects/<int:project_id>/launch-configs/create/",
        launch_configs.project_launch_config_create_ui,
        name="project_launch_config_create_ui",
    ),
]
