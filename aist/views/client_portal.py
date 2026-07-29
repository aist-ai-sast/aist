from __future__ import annotations

import json
import re
from typing import Any

from django.middleware.csrf import get_token
from django.shortcuts import render
from django.urls import reverse


def _replace_int_placeholder(url: str, name: str) -> str:
    return re.sub(r"/0(/|$)", rf"/{{{name}}}\1", url, count=1)


def _replace_str_placeholder(url: str, token: str, name: str) -> str:
    return url.replace(token, f"{{{name}}}")


def _build_routes() -> dict[str, Any]:
    return {
        "login_url": reverse("client_login"),
        "login_api_url": reverse("aist_api:auth_login"),
        "set_password_api_url": reverse("aist_api:auth_set_password"),
        "ui_set_password_path": "/auth/set-password/:uid/:token",
        "logout_url": reverse("aist_api:auth_logout"),
        "logout_all_devices_url": reverse("aist_api:auth_logout_all"),
        "me_url": reverse("aist_api:me"),
        "me_change_password_url": reverse("aist_api:me_change_password"),
        "findings_list_url": reverse("aist_api:finding_list"),
        "finding_timeline_url": reverse("aist_api:finding_timeline"),
        "finding_detail_url": _replace_int_placeholder(
            reverse("aist_api:finding_detail", kwargs={"finding_id": 0}),
            "id",
        ),
        "finding_notes_url": _replace_int_placeholder(
            reverse("aist_api:finding_notes", kwargs={"finding_id": 0}),
            "finding_id",
        ),
        "finding_risk_approval_url": _replace_int_placeholder(
            reverse("aist_api:finding_risk_approval", kwargs={"finding_id": 0}),
            "finding_id",
        ),
        "finding_close_url": _replace_int_placeholder(
            reverse("aist_api:finding_close", kwargs={"finding_id": 0}),
            "id",
        ),
        "finding_mark_duplicate_url": _replace_int_placeholder(
            reverse("aist_api:finding_mark_duplicate", kwargs={"finding_id": 0}),
            "finding_id",
        ),
        "finding_bulk_status_url": reverse("aist_api:finding_bulk_status"),
        "finding_export_url": _replace_int_placeholder(
            reverse("aist_api:finding_export", kwargs={"finding_id": 0}),
            "finding_id",
        ),
        "test_detail_url": _replace_int_placeholder(
            reverse("aist_api:test_detail", kwargs={"test_id": 0}),
            "id",
        ),
        "engagement_detail_url": _replace_int_placeholder(
            reverse("aist_api:engagement_detail", kwargs={"engagement_id": 0}),
            "id",
        ),
        "projects_list_url": reverse("aist_api:project_list"),
        "product_summary_url": reverse("client_product_summary"),
        "project_meta_url": _replace_int_placeholder(
            reverse("aist_api:project_meta", kwargs={"project_id": 0}),
            "project_id",
        ),
        "pipelines_list_url": reverse("aist_api:pipelines"),
        "pipelines_import_url": reverse("aist_api:aist_pipeline_import"),
        "pipelines_import_validate_url": reverse("aist_api:aist_pipeline_import_validate"),
        "pipeline_detail_url": _replace_str_placeholder(
            reverse("aist_api:pipeline_status", kwargs={"pipeline_id": "PIPELINE_ID"}),
            "PIPELINE_ID",
            "pipeline_id",
        ),
        "pipelines_summary_url": reverse("client_pipeline_summary"),
        "calendar_events_url": reverse("aist_api:calendar_events"),
        "calendar_event_detail_url": _replace_str_placeholder(
            reverse("aist_api:calendar_event_detail", kwargs={"event_id": "EVENT_ID"}),
            "EVENT_ID",
            "event_id",
        ),
        "pipeline_export_url": _replace_str_placeholder(
            reverse("aist_api:pipeline_export_ai_results", kwargs={"pipeline_id": "PIPELINE_ID"}),
            "PIPELINE_ID",
            "pipeline_id",
        ),
        "ai_finding_responses_url": reverse("aist_api:ai_finding_responses"),
        "finding_tags_url": reverse("aist_api:finding_tags"),
        "cwe_detail_url": _replace_int_placeholder(
            reverse("aist_api:cwe_detail", kwargs={"cwe_id": 0}),
            "cwe_id",
        ),
        "project_version_file_url": _replace_str_placeholder(
            _replace_int_placeholder(
                reverse(
                    "aist_api:project_version_file_blob",
                    kwargs={"project_version_id": 0, "subpath": "SUBPATH"},
                ),
                "project_version_id",
            ),
            "SUBPATH",
            "subpath",
        ),
        "project_version_file_prewarm_url": _replace_int_placeholder(
            reverse("aist_api:project_version_file_prewarm", kwargs={"project_version_id": 0}),
            "project_version_id",
        ),
        "dashboard_summary_url": reverse("client_dashboard_summary"),
        "manageable_orgs_url": reverse("aist_api:organization_create") + "?manage=true",
        "org_integrations_url": _replace_int_placeholder(
            reverse("aist_api:org_integration_list_create", kwargs={"org_id": 0}),
            "org_id",
        ),
        "org_integration_detail_url": _replace_int_placeholder(
            reverse("aist_api:org_integration_detail", kwargs={"integration_id": 0}),
            "integration_id",
        ),
        "dast_integration_disable_url": _replace_int_placeholder(
            reverse("aist_api:dast_integration_disable", kwargs={"integration_id": 0}),
            "integration_id",
        ),
        "org_integration_validate_url": _replace_int_placeholder(
            reverse("aist_api:org_integration_validate", kwargs={"integration_id": 0}),
            "integration_id",
        ),
        "org_integration_validate_status_url": _replace_str_placeholder(
            _replace_int_placeholder(
                reverse(
                    "aist_api:org_integration_validate_status",
                    kwargs={"integration_id": 0, "task_id": "TASKID"},
                ),
                "integration_id",
            ),
            "TASKID",
            "task_id",
        ),
        "dast_integration_import_url": _replace_int_placeholder(
            reverse("aist_api:organization_dast_integration_import", kwargs={"org_id": 0}),
            "org_id",
        ),
        "dast_integration_onboarding_url": _replace_int_placeholder(
            reverse("aist_api:dast_integration_onboarding_detail", kwargs={"integration_id": 0}),
            "integration_id",
        ),
        "dast_integration_rotate_token_url": _replace_int_placeholder(
            reverse("aist_api:dast_integration_rotate_token", kwargs={"integration_id": 0}),
            "integration_id",
        ),
        "organization_dast_target_catalog_url": _replace_int_placeholder(
            reverse("aist_api:organization_dast_target_catalog", kwargs={"org_id": 0}),
            "org_id",
        ),
        "dast_integration_sync_capabilities_url": _replace_int_placeholder(
            reverse("aist_api:dast_integration_sync_capabilities", kwargs={"integration_id": 0}),
            "integration_id",
        ),
        "project_dast_bindings_url": _replace_int_placeholder(
            reverse("aist_api:project_dast_binding_list_create", kwargs={"project_id": 0}),
            "project_id",
        ),
        "dast_binding_detail_url": _replace_int_placeholder(
            reverse("aist_api:project_dast_binding_detail", kwargs={"binding_id": 0}),
            "binding_id",
        ),
        "project_launch_configs_url": _replace_int_placeholder(
            reverse("aist_api:project_launch_config_list_create", kwargs={"project_id": 0}),
            "project_id",
        ),
        "project_integration_overrides_url": _replace_int_placeholder(
            reverse("aist_api:project_integration_overrides", kwargs={"project_id": 0}),
            "project_id",
        ),
        "project_integration_override_detail_url": _replace_str_placeholder(
            _replace_int_placeholder(
                reverse(
                    "aist_api:project_integration_override_detail",
                    kwargs={"project_id": 0, "integration_type": "TYPE"},
                ),
                "project_id",
            ),
            "TYPE",
            "integration_type",
        ),
        "ui_org_integrations_path": "/integrations",
        "org_members_url": _replace_int_placeholder(
            reverse("aist_api:org_member_list_create", kwargs={"org_id": 0}),
            "org_id",
        ),
        "org_member_detail_url": _replace_int_placeholder(
            _replace_int_placeholder(
                reverse("aist_api:org_member_detail", kwargs={"org_id": 0, "user_id": 0}),
                "org_id",
            ),
            "user_id",
        ),
        "org_member_reset_password_url": _replace_int_placeholder(
            _replace_int_placeholder(
                reverse("aist_api:org_member_reset_password", kwargs={"org_id": 0, "user_id": 0}),
                "org_id",
            ),
            "user_id",
        ),
        "org_member_reset_access_url": _replace_int_placeholder(
            _replace_int_placeholder(
                reverse("aist_api:org_member_reset_access", kwargs={"org_id": 0, "user_id": 0}),
                "org_id",
            ),
            "user_id",
        ),
        "org_member_project_grants_url": _replace_int_placeholder(
            _replace_int_placeholder(
                reverse("aist_api:org_member_project_grant_list_create", kwargs={"org_id": 0, "user_id": 0}),
                "org_id",
            ),
            "user_id",
        ),
        "org_member_project_grant_detail_url": _replace_int_placeholder(
            _replace_int_placeholder(
                _replace_int_placeholder(
                    reverse(
                        "aist_api:org_member_project_grant_detail",
                        kwargs={"org_id": 0, "user_id": 0, "project_id": 0},
                    ),
                    "org_id",
                ),
                "user_id",
            ),
            "project_id",
        ),
        "me_tokens_url": reverse("aist_api:me_token_list_create"),
        "me_token_detail_url": _replace_int_placeholder(
            reverse("aist_api:me_token_detail", kwargs={"token_id": 0}),
            "token_id",
        ),
        "admin_api_tokens_url": reverse("aist_api:admin_api_token_list"),
        "ui_users_path": "/users",
        "work_item_providers_url": _replace_int_placeholder(
            reverse("aist_api:work_item_provider_list_create", kwargs={"org_id": 0}),
            "org_id",
        ),
        "finding_work_items_url": _replace_int_placeholder(
            reverse("aist_api:finding_work_item_list_create", kwargs={"finding_id": 0}),
            "finding_id",
        ),
        "work_item_provider_detail_url": _replace_int_placeholder(
            reverse("aist_api:work_item_provider_detail", kwargs={"provider_id": 0}),
            "provider_id",
        ),
        "work_item_provider_validate_url": _replace_int_placeholder(
            reverse("aist_api:work_item_provider_validate", kwargs={"provider_id": 0}),
            "provider_id",
        ),
        "work_item_provider_validate_status_url": _replace_str_placeholder(
            _replace_int_placeholder(
                reverse(
                    "aist_api:work_item_provider_validate_status",
                    kwargs={"provider_id": 0, "task_id": "TASKID"},
                ),
                "provider_id",
            ),
            "TASKID",
            "task_id",
        ),
        "work_item_provider_sync_url": _replace_int_placeholder(
            reverse("aist_api:work_item_provider_sync", kwargs={"provider_id": 0}),
            "provider_id",
        ),
        "work_item_link_detail_url": _replace_int_placeholder(
            _replace_int_placeholder(
                reverse(
                    "aist_api:finding_work_item_detail",
                    kwargs={"finding_id": 0, "link_id": 0},
                ),
                "finding_id",
            ),
            "link_id",
        ),
        "ui_dashboard_path": "/dashboard",
        "ui_findings_path": reverse("findings"),
        "ui_finding_detail_path": "/findings/:id",
        "ui_products_path": "/products",
        "ui_pipelines_path": "/pipelines",
        "ui_calendar_path": "/calendar",
        "ui_search_path": "/search",
        "ui_settings_path": "/settings",
    }


def client_portal_index(request):
    routes = _build_routes()
    csrf_token = get_token(request)
    return render(
        request,
        "aist/client_portal.html",
        {
            "routes_json": json.dumps(routes),
            "csrf_token": csrf_token,
        },
    )
