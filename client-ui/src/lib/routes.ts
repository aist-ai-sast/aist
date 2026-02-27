type RouteMap = {
  login_url: string;
  login_api_url: string;
  dashboard_summary_url: string;
  ui_dashboard_path: string;
  logout_url: string;
  logout_all_devices_url: string;
  user_profile_url: string;
  me_url: string;
  me_change_password_url: string;
  findings_list_url: string;
  finding_detail_url: string;
  finding_notes_url: string;
  finding_close_url: string;
  finding_export_url: string;
  finding_tags_url: string;
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
  ui_findings_path: string;
  ui_finding_detail_path: string;
  ui_products_path: string;
  ui_pipelines_path: string;
  ui_calendar_path: string;
  ui_search_path: string;
  ui_settings_path: string;
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
