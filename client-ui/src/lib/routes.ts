type RouteMap = {
  login_url: string;
  logout_url: string;
  user_profile_url: string;
  findings_list_url: string;
  finding_detail_url: string;
  finding_notes_url: string;
  finding_close_url: string;
  projects_list_url: string;
  product_summary_url: string;
  project_meta_url: string;
  pipelines_list_url: string;
  pipelines_summary_url: string;
  pipeline_export_url: string;
  ai_finding_responses_url: string;
  project_version_file_url: string;
  ui_findings_path: string;
  ui_finding_detail_path: string;
  ui_products_path: string;
  ui_pipelines_path: string;
  ui_search_path: string;
  ui_settings_path: string;
};

declare global {
  interface Window {
    __AIST_ROUTES__?: RouteMap;
  }
}

const routes = window.__AIST_ROUTES__;

export function getRoute(name: keyof RouteMap, params?: Record<string, string | number>) {
  if (!routes) {
    throw new Error("Routes are not available.");
  }
  let url = routes[name];
  if (params) {
    Object.entries(params).forEach(([key, value]) => {
      url = url.replace(`{${key}}`, encodeURIComponent(String(value)));
      url = url.replace(`:${key}`, encodeURIComponent(String(value)));
    });
  }
  return url;
}
