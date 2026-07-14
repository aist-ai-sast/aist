import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { fetchJson, fetchText } from "./api";
import { getRoute } from "./routes";

export type AccountProfile = {
  username: string;
  first_name: string;
  last_name: string;
  email: string;
  can_edit_profile: boolean;
  can_edit_username: boolean;
  can_create_write_token: boolean;
  organization_memberships: Array<{
    organization_id: number;
    organization_name: string;
    role_id: number | null;
    role_name: string;
  }>;
};

export function useAccountProfile() {
  return useQuery({
    queryKey: ["account-profile"],
    queryFn: () => fetchJson<AccountProfile>(getRoute("me_url")),
  });
}

export function useUpdateAccountProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: Partial<Pick<AccountProfile, "first_name" | "last_name" | "email" | "username">>) =>
      fetchJson<AccountProfile>(getRoute("me_url"), {
        method: "PATCH",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["account-profile"] });
      queryClient.invalidateQueries({ queryKey: ["auth-status"] });
    },
  });
}

export function useChangePassword() {
  return useMutation({
    mutationFn: (payload: {
      current_password: string;
      new_password: string;
      new_password_confirm: string;
    }) =>
      fetchJson<{ ok: boolean }>(getRoute("me_change_password_url"), {
        method: "POST",
        body: JSON.stringify(payload),
      }),
  });
}

export function logoutAllDevices() {
  return fetchText(getRoute("logout_all_devices_url"), { method: "POST" });
}
