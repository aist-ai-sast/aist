import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { fetchJson, normalizeList } from "./api";
import { getRoute } from "./routes";

export type MembershipType = "full" | "restricted";

export type ProjectGrant = {
  project_id: number;
  product_id: number;
  project_name: string;
  role_id: number;
  role_name: string;
};

export type OrgMember = {
  user_id: number;
  username: string;
  email: string;
  first_name: string;
  last_name: string;
  is_active: boolean;
  role_id: number | null;
  role_name: string;
  membership_type: MembershipType;
  has_token: boolean;
  token_count: number;
  project_grants: ProjectGrant[];
};

// Platform role ids. Reader/Writer/Maintainer/Owner are the roles an org admin
// assigns; API_Importer is intentionally omitted from the management UI.
export const ROLE_OPTIONS = [
  { id: 5, name: "Reader" },
  { id: 2, name: "Writer" },
  { id: 3, name: "Maintainer" },
  { id: 4, name: "Owner" },
] as const;

export type InvitePayload = {
  email: string;
  first_name?: string;
  last_name?: string;
  role_id?: number;
  project_grants?: { project_id: number; role_id: number }[];
};

const membersKey = (orgId: number) => ["org-members", orgId];

export function useOrgMembers(orgId?: number) {
  return useQuery({
    queryKey: membersKey(orgId ?? 0),
    enabled: !!orgId,
    queryFn: async () => {
      const payload = await fetchJson<OrgMember[] | { results?: OrgMember[] }>(
        getRoute("org_members_url", { org_id: orgId! }),
      );
      return normalizeList(payload);
    },
  });
}

function useMembersMutation<TVars>(
  orgId: number,
  mutationFn: (vars: TVars) => Promise<unknown>,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: membersKey(orgId) }),
  });
}

export function useInviteMember(orgId: number) {
  return useMembersMutation<InvitePayload>(orgId, (payload) =>
    fetchJson(getRoute("org_members_url", { org_id: orgId }), {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}

export function useChangeMemberRole(orgId: number) {
  return useMembersMutation<{ userId: number; roleId: number }>(orgId, ({ userId, roleId }) =>
    fetchJson(getRoute("org_member_detail_url", { org_id: orgId, user_id: userId }), {
      method: "PATCH",
      body: JSON.stringify({ role_id: roleId }),
    }),
  );
}

export function useRemoveMember(orgId: number) {
  return useMembersMutation<number>(orgId, (userId) =>
    fetchJson(getRoute("org_member_detail_url", { org_id: orgId, user_id: userId }), { method: "DELETE" }),
  );
}

export function useResetMemberPassword(orgId: number) {
  return useMembersMutation<number>(orgId, (userId) =>
    fetchJson(getRoute("org_member_reset_password_url", { org_id: orgId, user_id: userId }), { method: "POST" }),
  );
}

export function useGrantProject(orgId: number) {
  return useMembersMutation<{ userId: number; projectId: number; roleId: number }>(
    orgId,
    ({ userId, projectId, roleId }) =>
      fetchJson(getRoute("org_member_project_grants_url", { org_id: orgId, user_id: userId }), {
        method: "POST",
        body: JSON.stringify({ project_id: projectId, role_id: roleId }),
      }),
  );
}

export function useRevokeProject(orgId: number) {
  return useMembersMutation<{ userId: number; projectId: number }>(orgId, ({ userId, projectId }) =>
    fetchJson(
      getRoute("org_member_project_grant_detail_url", {
        org_id: orgId,
        user_id: userId,
        project_id: projectId,
      }),
      { method: "DELETE" },
    ),
  );
}
