import type { ReactNode } from "react";

export function getRoleBadgeClass(roleName: string): string {
  const normalized = roleName.trim().toLowerCase();
  if (normalized === "owner" || normalized === "superuser") return "border-brand-500/60 bg-brand-500/15 text-brand-100";
  if (normalized === "maintainer") return "border-emerald-500/40 bg-emerald-500/10 text-emerald-100";
  if (normalized === "writer" || normalized === "api importer") return "border-sky-500/40 bg-sky-500/10 text-sky-100";
  return "border-night-400/70 bg-night-700/70 text-slate-200";
}

export function getRoleIconClass(roleName: string): string {
  const normalized = roleName.trim().toLowerCase();
  if (normalized === "owner" || normalized === "superuser") return "text-brand-200";
  if (normalized === "maintainer") return "text-emerald-200";
  if (normalized === "writer" || normalized === "api importer") return "text-sky-200";
  return "text-slate-300";
}

export function getRoleIcon(roleName: string): ReactNode {
  const normalized = roleName.trim().toLowerCase();
  if (normalized === "owner" || normalized === "superuser") {
    return (
      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 shrink-0" aria-hidden="true">
        <path d="M3.5 9.5 7 13l5-7 5 7 3.5-3.5V18a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2V9.5Z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  if (normalized === "maintainer") {
    return (
      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 shrink-0" aria-hidden="true">
        <path d="M12 3 5 6v5c0 4.3 2.9 8 7 9.9 4.1-1.9 7-5.6 7-9.9V6l-7-3Z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        <path d="m9.5 12 1.7 1.7L14.8 10" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  if (normalized === "writer") {
    return (
      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 shrink-0" aria-hidden="true">
        <path d="m4 20 3.8-.8L18 9 15 6 4.8 16.2 4 20Z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  if (normalized === "api importer") {
    return (
      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 shrink-0" aria-hidden="true">
        <path d="M8 7h11M8 12h11M8 17h11M4 7l1.5-1.5M4 17l1.5 1.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 shrink-0" aria-hidden="true">
      <path d="M4 12s3-5 8-5 8 5 8 5-3 5-8 5-8-5-8-5Z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="12" cy="12" r="2.3" fill="none" stroke="currentColor" strokeWidth="1.8" />
    </svg>
  );
}
