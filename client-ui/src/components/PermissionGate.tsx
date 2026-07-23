import type { ReactNode } from "react";

import { usePermissions, type PermissionAction } from "../lib/permissions";

type PermissionGateProps = {
  action: PermissionAction;
  fallback?: ReactNode;
  loadingFallback?: ReactNode;
  children: ReactNode;
  organizationId?: number;
};

const actionMap: Record<PermissionAction, (perms: ReturnType<typeof usePermissions>) => boolean> = {
  write: (perms) => perms.canWrite,
  operate_project: (perms) => perms.canOperateProject,
  comment: (perms) => perms.canComment,
  enable: (perms) => perms.canEnable,
  manage_access: (perms) => perms.canManageAccess,
};

export default function PermissionGate({
  action,
  fallback = null,
  loadingFallback = null,
  children,
  organizationId,
}: PermissionGateProps) {
  const perms = usePermissions(organizationId);
  if (perms.isLoading) return <>{loadingFallback}</>;
  return actionMap[action](perms) ? <>{children}</> : <>{fallback}</>;
}
