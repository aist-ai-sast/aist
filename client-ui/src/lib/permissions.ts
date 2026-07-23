import { useMemo } from "react";

import type { UserProfile } from "./auth";
import { useAuthStatus } from "./auth";

const RoleIds = {
  API_Importer: 1,
  Writer: 2,
  Maintainer: 3,
  Owner: 4,
  Reader: 5,
} as const;

export const roleRank: Record<number, number> = {
  [RoleIds.Reader]: 0,
  [RoleIds.API_Importer]: 1,
  [RoleIds.Writer]: 2,
  [RoleIds.Maintainer]: 3,
  [RoleIds.Owner]: 4,
};

function getBestRole(roles: (number | undefined)[]): number | null {
  const valid = roles.filter((r): r is number => r != null);
  if (!valid.length) return null;
  return valid.reduce((best, current) => (roleRank[current] ?? -1) > (roleRank[best] ?? -1) ? current : best);
}

function getRoleFromProfile(profile?: UserProfile | null, organizationId?: number): number | "superuser" | null {
  if (!profile) return null;
  if (profile.is_superuser) return "superuser";

  if (profile.organization_memberships?.length) {
    if (organizationId !== undefined) {
      return profile.organization_memberships.find((membership) => membership.organization_id === organizationId)?.role_id ?? null;
    }
    const best = getBestRole(
      profile.organization_memberships.map((m) => m.role_id ?? undefined),
    );
    if (best !== null) return best;
  }

  return null;
}

function canWriteWithRole(role: number | "superuser" | null): boolean {
  if (role === "superuser") return true;
  if (!role) return false;
  return role === RoleIds.Writer || role === RoleIds.Maintainer || role === RoleIds.Owner;
}

function canManageAccess(role: number | "superuser" | null): boolean {
  if (role === "superuser") return true;
  if (!role) return false;
  return role === RoleIds.Maintainer || role === RoleIds.Owner;
}

export type PermissionAction = "write" | "operate_project" | "comment" | "enable" | "manage_access";

export function usePermissions(organizationId?: number) {
  const auth = useAuthStatus();
  const membership = useMemo(() => {
    const memberships = auth.data?.organization_memberships ?? [];
    if (organizationId !== undefined) {
      return memberships.find((item) => item.organization_id === organizationId) ?? null;
    }
    return null;
  }, [auth.data, organizationId]);
  const role = useMemo(() => {
    if (!auth.data) return null;
    return getRoleFromProfile(auth.data, organizationId);
  }, [auth.data, organizationId]);

  const canWrite = useMemo(
    () => membership?.can_write_findings ?? canWriteWithRole(role),
    [membership, role],
  );
  const canOperateProject = membership?.can_operate_projects
    ?? (role === "superuser" || role === RoleIds.Maintainer || role === RoleIds.Owner);
  const canManage = membership?.can_manage_access ?? canManageAccess(role);

  return {
    canWrite,
    canOperateProject,
    canComment: canWrite,
    canEnable: canWrite,
    canManageAccess: canManage,
    isLoading: auth.isLoading,
  };
}

export function useWritePermissions() {
  const permissions = usePermissions();
  return {
    canWrite: permissions.canWrite,
    isLoading: permissions.isLoading,
  };
}
