import type { ReactNode } from "react";

import { usePermissions, type PermissionAction } from "../lib/permissions";

type PermissionGateProps = {
  action: PermissionAction;
  fallback?: ReactNode;
  loadingFallback?: ReactNode;
  children: ReactNode;
};

const actionMap: Record<PermissionAction, (perms: ReturnType<typeof usePermissions>) => boolean> = {
  write: (perms) => perms.canWrite,
  comment: (perms) => perms.canComment,
  enable: (perms) => perms.canEnable,
  export: (perms) => perms.canExport,
  manage_access: (perms) => perms.canManageAccess,
};

export default function PermissionGate({
  action,
  fallback = null,
  loadingFallback = null,
  children,
}: PermissionGateProps) {
  const perms = usePermissions();
  if (perms.isLoading) return <>{loadingFallback}</>;
  return actionMap[action](perms) ? <>{children}</> : <>{fallback}</>;
}
