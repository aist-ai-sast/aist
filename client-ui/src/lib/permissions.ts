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

function getRoleFromProfile(profile?: UserProfile | null, productId?: number): number | "superuser" | null {
  if (!profile) return null;
  if (profile.user?.is_superuser) return "superuser";

  if (profile.product_member?.length) {
    const members = productId
      ? profile.product_member.filter((member) => member.product === productId && member.role)
      : profile.product_member.filter((member) => member.role);
    if (members.length) {
      return members
        .map((member) => member.role as number)
        .reduce((best, current) => (roleRank[current] ?? -1) > (roleRank[best] ?? -1) ? current : best);
    }
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

export function usePermissions(productId?: number) {
  const auth = useAuthStatus();
  const role = useMemo(() => {
    if (!auth.data) return null;
    return getRoleFromProfile(auth.data, productId);
  }, [auth.data, productId]);

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

export function useWritePermissions(productId?: number) {
  const permissions = usePermissions(productId);
  return {
    canWrite: permissions.canWrite,
    isLoading: permissions.isLoading,
  };
}
