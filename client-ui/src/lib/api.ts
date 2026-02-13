import { getRoute } from "./routes";

export function getCookie(name: string) {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(new RegExp(`(^| )${name}=([^;]+)`));
  return match ? decodeURIComponent(match[2]) : null;
}

declare global {
  interface Window {
    __AIST_CSRF__?: string;
  }
}

export function getCsrfToken() {
  return getCookie("csrftoken") ?? window.__AIST_CSRF__ ?? null;
}

function buildHeaders(init?: RequestInit) {
  const method = init?.method?.toUpperCase() ?? "GET";
  const headers = new Headers(init?.headers);
  if (!headers.has("Content-Type") && method !== "GET") {
    headers.set("Content-Type", "application/json");
  }
  if (method !== "GET") {
    const csrf = getCsrfToken();
    if (csrf) {
      headers.set("X-CSRFToken", csrf);
    }
  }
  return headers;
}

export async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, {
    ...init,
    headers: buildHeaders(init),
    credentials: "include",
  });
  if (!resp.ok) {
    throw new Error(`Request failed: ${resp.status}`);
  }
  return resp.json() as Promise<T>;
}

export async function fetchText(url: string, init?: RequestInit): Promise<string> {
  const resp = await fetch(url, {
    ...init,
    headers: buildHeaders(init),
    credentials: "include",
  });
  if (!resp.ok) {
    throw new Error(`Request failed: ${resp.status}`);
  }
  return resp.text();
}

export async function fetchBlob(url: string, init?: RequestInit): Promise<Response> {
  const resp = await fetch(url, {
    ...init,
    headers: buildHeaders(init),
    credentials: "include",
  });
  if (!resp.ok) {
    throw new Error(`Request failed: ${resp.status}`);
  }
  return resp;
}

export async function fetchFileContent(projectVersionIdOrUrl: number | string, filePath?: string) {
  if (typeof projectVersionIdOrUrl === "string") {
    return fetchText(projectVersionIdOrUrl);
  }
  const encodedPath = (filePath ?? "").split("/").map(encodeURIComponent).join("/");
  return fetchText(
    getRoute("project_version_file_url", {
      project_version_id: projectVersionIdOrUrl,
      subpath: encodedPath,
    }),
  );
}

export function normalizeList<T>(payload: { results?: T[] } | T[]): T[] {
  if (Array.isArray(payload)) {
    return payload;
  }
  return payload.results ?? [];
}
