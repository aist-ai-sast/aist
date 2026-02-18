import type { ProjectVersionType } from "../types";

export function formatProjectVersionLabel(
  value?: string | null,
  versionType?: ProjectVersionType,
  hashLength = 8,
): string {
  if (!value) return "";
  if (versionType !== "GIT_HASH") return value;
  return value.slice(0, hashLength);
}

export function projectVersionTypeLabel(versionType?: ProjectVersionType): string {
  if (versionType === "GIT_HASH") return "Git hash";
  if (versionType === "GIT_BRANCH") return "Git branch";
  if (versionType === "FILE_HASH") return "File hash";
  return "Version";
}

export function formatProjectVersionText(
  value?: string | null,
  versionType?: ProjectVersionType,
): string {
  const normalized = formatProjectVersionLabel(value, versionType);
  if (!normalized) return projectVersionTypeLabel(versionType);
  return `${projectVersionTypeLabel(versionType)}: ${normalized}`;
}
