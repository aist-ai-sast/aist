import type { UserProfile } from "./auth";

export function getDisplayName(profile?: UserProfile | null): string {
  if (!profile) return "Unknown user";
  const first = profile.first_name?.trim() ?? "";
  const last = profile.last_name?.trim() ?? "";
  const full = [first, last].filter(Boolean).join(" ").trim();
  if (full) return full;
  return profile.username || "Unknown user";
}

export function getUsername(profile?: UserProfile | null): string {
  if (!profile?.username) return "unknown";
  return profile.username;
}

export function getRoleLabel(profile?: UserProfile | null): "Admin" | "Client" {
  if (profile?.is_superuser) return "Admin";
  return "Client";
}

export function getInitials(profile?: UserProfile | null): string {
  const name = getDisplayName(profile).trim();
  if (!name) return "U";
  const parts = name.split(/\s+/).filter(Boolean);
  if (parts.length === 1) {
    return parts[0].slice(0, 2).toUpperCase();
  }
  return `${parts[0][0] ?? ""}${parts[1][0] ?? ""}`.toUpperCase();
}
