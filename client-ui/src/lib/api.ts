import axios, { AxiosError, type AxiosRequestConfig, type AxiosResponse } from "axios";

import { getRoute } from "./routes";

export const AUTH_EXPIRED_EVENT = "aist:auth-expired";

type ApiErrorCode = "auth_expired" | "forbidden" | "http_error";

export class ApiError extends Error {
  status: number;
  code: ApiErrorCode;
  payload: unknown;
  url: string;

  constructor(params: {
    status: number;
    code: ApiErrorCode;
    payload: unknown;
    url: string;
  }) {
    super(`Request failed: ${params.status}`);
    this.name = "ApiError";
    this.status = params.status;
    this.code = params.code;
    this.payload = params.payload;
    this.url = params.url;
  }
}

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

function headersToObject(headers: Headers): Record<string, string> {
  const result: Record<string, string> = {};
  headers.forEach((value, key) => {
    result[key] = value;
  });
  return result;
}

function extractErrorDetail(payload: unknown): string {
  if (!payload) return "";
  if (typeof payload === "string") return payload;
  if (typeof payload === "object" && payload !== null) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      const first = detail[0];
      return typeof first === "string" ? first : "";
    }
    if (Array.isArray(payload)) {
      const first = payload[0];
      return typeof first === "string" ? first : "";
    }
  }
  return "";
}

function emitAuthExpired(url: string, status: number) {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(AUTH_EXPIRED_EVENT, { detail: { url, status } }));
}

function isAuthExpiredStatus(status: number, payload: unknown, url: string): boolean {
  if (status === 401) return true;
  if (status !== 403) return false;
  try {
    const authStatusUrls = [getRoute("user_profile_url"), getRoute("me_url")];
    if (authStatusUrls.some((endpoint) => url.includes(endpoint))) {
      return true;
    }
  } catch {
    // Ignore route bootstrap issues; fallback to payload heuristics below.
  }
  const detail = extractErrorDetail(payload).toLowerCase();
  if (!detail) return false;
  return detail.includes("authentication credentials were not provided")
    || detail.includes("not authenticated")
    || detail.includes("csrf")
    || detail.includes("session");
}

function toApiError(error: AxiosError): ApiError {
  const status = error.response?.status ?? 0;
  const payload = error.response?.data ?? null;
  const url = error.config?.url ?? "";
  const code: ApiErrorCode = isAuthExpiredStatus(status, payload, url)
    ? "auth_expired"
    : status === 403
      ? "forbidden"
      : "http_error";
  if (code === "auth_expired") {
    emitAuthExpired(url, status);
  }
  return new ApiError({
    status,
    code,
    payload,
    url,
  });
}

const apiClient = axios.create({
  withCredentials: true,
});

apiClient.interceptors.response.use(
  (response) => response,
  (error: unknown) => {
    if (axios.isAxiosError(error)) {
      return Promise.reject(toApiError(error));
    }
    return Promise.reject(error);
  },
);

async function request<T = unknown>(
  url: string,
  init?: RequestInit,
  responseType: AxiosRequestConfig["responseType"] = "json",
): Promise<AxiosResponse<T>> {
  const method = init?.method?.toUpperCase() ?? "GET";
  const headers = headersToObject(buildHeaders(init));
  const config: AxiosRequestConfig = {
    url,
    method,
    headers,
    responseType,
  };
  if (init?.body !== undefined) {
    config.data = init.body;
  }
  return apiClient.request<T>(config);
}

export function isAuthExpiredError(error: unknown): boolean {
  return error instanceof ApiError && error.code === "auth_expired";
}

export function isAccessDeniedError(error: unknown): boolean {
  return error instanceof ApiError && error.code === "forbidden";
}

export function toUserMessage(error: unknown): string {
  if (isAuthExpiredError(error)) {
    return "Session expired, please login again.";
  }
  if (isAccessDeniedError(error)) {
    return "Access denied.";
  }
  if (error instanceof ApiError) {
    if (error.status >= 500) {
      return "Server error. Please try again later.";
    }
    const detail = extractErrorDetail(error.payload);
    if (detail) return detail;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "Unexpected error";
}

export async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await request<T>(url, init, "json");
  return resp.data;
}

export async function fetchText(url: string, init?: RequestInit): Promise<string> {
  const resp = await request<string>(url, init, "text");
  return resp.data;
}

export async function fetchBlob(url: string, init?: RequestInit): Promise<AxiosResponse<Blob>> {
  return request<Blob>(url, init, "blob");
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
