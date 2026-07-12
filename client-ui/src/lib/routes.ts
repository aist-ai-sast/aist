type RouteMap = {
  login_url: string;
  login_api_url: string;
  set_password_api_url: string;
  ui_set_password_path: string;
  dashboard_summary_url: string;
  work_item_providers_url: string;
  finding_work_items_url: string;
  work_item_link_detail_url: string;
  work_item_provider_detail_url: string;
  work_item_provider_validate_url: string;
  work_item_provider_validate_status_url: string;
  work_item_provider_sync_url: string;
  ui_dashboard_path: string;
  logout_url: string;
  logout_all_devices_url: string;
  user_profile_url: string;
  me_url: string;
  me_change_password_url: string;
  findings_list_url: string;
  finding_timeline_url: string;
  finding_detail_url: string;
  finding_notes_url: string;
  finding_risk_approval_url: string;
  finding_close_url: string;
  finding_mark_duplicate_url: string;
  finding_bulk_status_url: string;
  finding_export_url: string;
  finding_tags_url: string;
  cwe_detail_url: string;
  test_detail_url: string;
  engagement_detail_url: string;
  projects_list_url: string;
  product_summary_url: string;
  project_meta_url: string;
  pipelines_list_url: string;
  pipelines_summary_url: string;
  pipeline_export_url: string;
  calendar_events_url: string;
  calendar_event_detail_url: string;
  ai_finding_responses_url: string;
  project_version_file_url: string;
  project_version_file_prewarm_url: string;
  ui_findings_path: string;
  ui_finding_detail_path: string;
  ui_products_path: string;
  ui_pipelines_path: string;
  ui_calendar_path: string;
  ui_search_path: string;
  ui_settings_path: string;
  ui_org_integrations_path: string;
  ui_users_path: string;
  org_members_url: string;
  org_member_detail_url: string;
  org_member_reset_password_url: string;
  org_member_project_grants_url: string;
  org_member_project_grant_detail_url: string;
  me_tokens_url: string;
  me_token_detail_url: string;
  admin_api_tokens_url: string;
  manageable_orgs_url: string;
  org_integrations_url: string;
  org_integration_detail_url: string;
  org_integration_validate_url: string;
  org_integration_validate_status_url: string;
  project_integration_overrides_url: string;
  project_integration_override_detail_url: string;
};

type RouteUrlKey = {
  [K in keyof RouteMap]: RouteMap[K] extends string ? K : never;
}[keyof RouteMap];

declare global {
  interface Window {
    __AIST_ROUTES__?: RouteMap;
  }
}

const routes = window.__AIST_ROUTES__;

export function getRoute(name: RouteUrlKey, params?: Record<string, string | number>) {
  if (!routes) {
    throw new Error("Routes are not available.");
  }
  let url = routes[name] as string;
  if (params) {
    Object.entries(params).forEach(([key, value]) => {
      url = url.replace(`{${key}}`, encodeURIComponent(String(value)));
      url = url.replace(`:${key}`, encodeURIComponent(String(value)));
    });
  }
  return url;
}
