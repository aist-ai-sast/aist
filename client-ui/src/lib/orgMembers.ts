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
  denied_project_ids: number[];
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

// "existing_user_added_no_email": the email already belongs to an active user
// elsewhere on the platform — they're added to this org with their existing
// credentials, no email is sent. Callers must not show a generic "invitation
// sent" success message for this outcome.
export type InviteStatus = "invited" | "existing_user_added_no_email";

export type InviteResult = {
  user_id: number;
  username: string;
  email: string;
  invite_status: InviteStatus;
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

function useMembersMutation<TVars, TResult = unknown>(
  orgId: number,
  mutationFn: (vars: TVars) => Promise<TResult>,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: membersKey(orgId) }),
  });
}

export function useInviteMember(orgId: number) {
  return useMembersMutation<InvitePayload, InviteResult>(orgId, (payload) =>
    fetchJson<InviteResult>(getRoute("org_members_url", { org_id: orgId }), {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}

// The backend distinguishes a real invite from silently adding an
// already-active user with no email sent — callers must not show the same
// generic "success" message for both, or a no-email add reads as if an
// invite email went out when it didn't.
export function inviteResultMessage(result: InviteResult): string {
  if (result.invite_status === "existing_user_added_no_email") {
    return `${result.email} already has an account and was added with their existing credentials — no email was sent.`;
  }
  return "Invitation sent.";
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

// The only path back to full access for a restricted member — narrowing
// happens implicitly via grant/revoke, but broadening always requires this
// explicit, deliberate action (never a side effect of an emptied grant list).
export function useResetOrgMemberAccess(orgId: number) {
  return useMembersMutation<number>(orgId, (userId) =>
    fetchJson(getRoute("org_member_reset_access_url", { org_id: orgId, user_id: userId }), { method: "POST" }),
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
