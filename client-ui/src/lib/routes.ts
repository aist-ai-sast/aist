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
  project_version_file_url: string;
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
    });
  }
  return url;
}
