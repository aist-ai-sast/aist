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

const roleRank: Record<number, number> = {
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

function getRoleFromProfile(profile?: UserProfile | null): number | "superuser" | null {
  if (!profile) return null;
  if (profile.user?.is_superuser) return "superuser";

  if (profile.product_type_member?.length) {
    const best = getBestRole(
      profile.product_type_member.filter((m) => m.role).map((m) => m.role),
    );
    if (best !== null) return best;
  }

  if (profile.global_role?.role) {
    return profile.global_role.role;
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

export type PermissionAction = "write" | "comment" | "enable" | "export" | "manage_access";

export function usePermissions() {
  const auth = useAuthStatus();
  const role = useMemo(() => {
    if (!auth.data) return null;
    return getRoleFromProfile(auth.data);
  }, [auth.data]);

  const canWrite = useMemo(() => canWriteWithRole(role), [role]);

  return {
    canWrite,
    canComment: canWrite,
    canEnable: canWrite,
    canExport: true,
    canManageAccess: canManageAccess(role),
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
