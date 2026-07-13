import { useQuery } from "@tanstack/react-query";

import { ApiError, fetchJson, fetchText } from "./api";
import { getRoute } from "./routes";

export type OrganizationMembership = {
  organization_id: number;
  organization_name: string;
  role_id: number | null;
  role_name: string;
};

export type UserProfile = {
  username: string;
  first_name?: string;
  last_name?: string;
  email?: string;
  is_superuser?: boolean;
  organization_memberships?: OrganizationMembership[];
};

export async function loginWithSession(username: string, password: string) {
  // Refresh CSRF token/cookie for anonymous session before login attempt.
  await fetchText(getRoute("login_url"));

  const doLogin = () => fetchText(getRoute("login_api_url"), {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });

  try {
    await doLogin();
  } catch (error) {
    if (error instanceof ApiError && error.status === 403) {
      // Retry once with freshly issued CSRF token.
      await fetchText(getRoute("login_url"));
      await doLogin();
    } else if (error instanceof ApiError && (error.status === 400 || error.status === 401)) {
      throw new Error("Invalid username or password.");
    } else {
      throw error;
    }
  }

  try {
    await fetchJson<UserProfile>(getRoute("me_url"));
  } catch {
    throw new Error("Invalid username or password.");
  }
}

export async function logoutSession() {
  await fetchText(getRoute("logout_url"), { method: "POST" });
}

export function useAuthStatus(enabled = true) {
  return useQuery({
    queryKey: ["auth-status"],
    queryFn: () => fetchJson<UserProfile>(getRoute("me_url")),
    enabled,
    staleTime: 60 * 1000,
    retry: false,
    refetchOnWindowFocus: true,
    refetchOnReconnect: true,
  });
}
