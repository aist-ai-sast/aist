import type { ReactNode } from "react";

import { PROVIDER_ICON_PATHS } from "../lib/providerIcons";

export const TYPE_LABELS: Record<string, string> = {
  GITLAB: "GitLab",
  GITHUB: "GitHub",
  GERRIT: "Gerrit",
  GITEA: "Gitea",
  SLACK: "Slack",
  EMAIL: "Email",
  VPN: "VPN",
  CLAUDE_CODE: "Claude Code",
  DAST: "DAST",
  JIRA: "Jira",
  YOUTRACK: "YouTrack",
  LINEAR: "Linear",
  AZURE_DEVOPS: "Azure DevOps",
  GENERIC: "Generic",
};

const TYPE_BADGE_CLASSES: Record<string, string> = {
  GITLAB: "border-orange-500/40 bg-orange-500/10 text-orange-300",
  GITHUB: "border-slate-400/40 bg-slate-400/10 text-slate-300",
  GERRIT: "border-red-500/40 bg-red-500/10 text-red-300",
  GITEA: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  SLACK: "border-green-500/40 bg-green-500/10 text-green-300",
  EMAIL: "border-brand-500/40 bg-brand-500/10 text-brand-300",
  JIRA: "border-blue-500/40 bg-blue-500/10 text-blue-300",
  YOUTRACK: "border-purple-500/40 bg-purple-500/10 text-purple-300",
  LINEAR: "border-indigo-500/40 bg-indigo-500/10 text-indigo-300",
  AZURE_DEVOPS: "border-cyan-500/40 bg-cyan-500/10 text-cyan-300",
  GENERIC: "border-slate-400/30 bg-slate-400/10 text-slate-400",
  VPN: "border-slate-400/40 bg-slate-400/10 text-slate-300",
  CLAUDE_CODE: "border-amber-500/40 bg-amber-500/10 text-amber-300",
  DAST: "border-rose-500/40 bg-rose-500/10 text-rose-300",
};

export function ProviderIcon({ type }: { type: string }) {
  const d = PROVIDER_ICON_PATHS[type];
  if (!d) return null;
  return (
    <svg viewBox="0 0 24 24" className="h-3 w-3 shrink-0" aria-hidden="true">
      <path fill="currentColor" d={d} />
    </svg>
  );
}

export function TypeBadge({ type }: { type: string }) {
  const cls = TYPE_BADGE_CLASSES[type] ?? TYPE_BADGE_CLASSES.GENERIC;
  return (
    <span className={`inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${cls}`}>
      <ProviderIcon type={type} />
      {TYPE_LABELS[type] ?? type}
    </span>
  );
}

export function SectionCard({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <section className="aist-card border-night-500/80 p-0">
      <div className="border-b border-night-500/70 px-5 py-4">
        <div className="text-xs uppercase tracking-[0.2em] text-slate-300">{title}</div>
        {description && <p className="mt-0.5 text-xs text-slate-500">{description}</p>}
      </div>
      <div className="px-5 py-4 space-y-3">{children}</div>
    </section>
  );
}

export function AddButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      className="aist-icon-button border-brand-500/50 bg-brand-500/15 text-brand-100 hover:border-brand-400/70 hover:bg-brand-500/25"
      onClick={onClick}
    >
      <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
        <path fill="currentColor" d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2Z" />
      </svg>
      Add
    </button>
  );
}
