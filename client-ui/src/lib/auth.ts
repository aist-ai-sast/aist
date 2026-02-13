import { useQuery } from "@tanstack/react-query";

import { fetchJson, getCookie, getCsrfToken } from "./api";
import { getRoute } from "./routes";

export type UserProfile = {
  user: {
    username: string;
    first_name?: string;
    last_name?: string;
    email?: string;
    is_superuser?: boolean;
  };
  global_role?: {
    role?: number;
  } | null;
  product_member?: Array<{
    product?: number;
    role?: number;
  }>;
};

export async function loginWithSession(username: string, password: string) {
  const loginUrl = getRoute("login_url");
  await fetch(loginUrl, { credentials: "include" });
  const csrf = getCookie("csrftoken");
  const body = new URLSearchParams({
    username,
    password,
    csrfmiddlewaretoken: csrf ?? getCsrfToken() ?? "",
    next: "/",
  });

  const resp = await fetch(loginUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      ...(csrf || getCsrfToken() ? { "X-CSRFToken": csrf ?? getCsrfToken() ?? "" } : {}),
    },
    body,
    credentials: "include",
  });

  if (!resp.ok) {
    let message = `Login failed: ${resp.status}`;
    try {
      const payload = (await resp.json()) as { detail?: string };
      if (payload.detail) {
        message = payload.detail;
      }
    } catch {
      // ignore parse errors
    }
    throw new Error(message);
  }

  try {
    await fetchJson<UserProfile>(getRoute("user_profile_url"));
  } catch {
    throw new Error("Invalid username or password.");
  }
}

export async function logoutSession() {
  await fetch(getRoute("logout_url"), { credentials: "include" });
}

export function useAuthStatus() {
  return useQuery({
    queryKey: ["auth-status"],
    queryFn: () => fetchJson<UserProfile>(getRoute("user_profile_url")),
    retry: 2,
    retryDelay: 500,
    refetchOnWindowFocus: false,
  });
}
