import type { ReactNode } from "react";

import type { IntegrationType } from "../lib/integrationTypes";
import { PROVIDER_ICON_PATHS } from "../lib/providerIcons";
import PermissionGate from "./PermissionGate";

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
  BUILD_ENV: "Build environment",
  JIRA: "Jira",
  YOUTRACK: "YouTrack",
  LINEAR: "Linear",
  AZURE_DEVOPS: "Azure DevOps",
  GENERIC: "Generic",
} satisfies Record<IntegrationType, string> & Record<string, string>;

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
  BUILD_ENV: "border-teal-500/40 bg-teal-500/10 text-teal-300",
} satisfies Record<IntegrationType, string> & Record<string, string>;

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

function LockIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
      <path fill="currentColor" d="M18 8h-1V6a5 5 0 0 0-10 0v2H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V10a2 2 0 0 0-2-2Zm-6 9a2 2 0 1 1 0-4 2 2 0 0 1 0 4Zm3.1-9H8.9V6a3.1 3.1 0 0 1 6.2 0v2Z" />
    </svg>
  );
}

export function ResourceRow({
  typeKey,
  name,
  hasSecret,
  isActive,
  isDefault,
  vpnName,
  onEdit,
  onDelete,
  onValidate,
  onSynchronize,
  isPendingDelete,
  isPendingValidate,
  isPendingSynchronize,
  organizationId,
  statusLabel,
  fingerprint,
  destructiveLabel = "Delete",
  destructiveTitle = "Delete",
}: {
  typeKey: string;
  name: string;
  hasSecret: boolean;
  isActive: boolean;
  isDefault?: boolean;
  vpnName?: string;
  onEdit: () => void;
  onDelete: () => void;
  onValidate: () => void;
  onSynchronize?: () => void;
  isPendingDelete: boolean;
  isPendingValidate: boolean;
  isPendingSynchronize?: boolean;
  organizationId: number;
  statusLabel?: string;
  fingerprint?: string;
  destructiveLabel?: string;
  destructiveTitle?: string;
}) {
  return (
    <div className="flex flex-col gap-2 rounded-2xl border border-night-500/80 bg-night-800/75 px-4 py-3 sm:flex-row sm:items-center sm:gap-3">
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1.5">
        <TypeBadge type={typeKey} />
        <span className="min-w-0 flex-1 basis-32 truncate text-sm font-medium text-slate-100" title={name}>{name}</span>
        {statusLabel && <span className="text-[10px] uppercase tracking-wide text-slate-400">{statusLabel}</span>}
        {fingerprint && <span className="max-w-40 truncate font-mono text-[10px] text-slate-500" title={fingerprint}>{fingerprint}</span>}
        <div className="flex min-w-0 max-w-full flex-wrap items-center gap-1.5 text-[11px]">
          {vpnName && (
            <span className="flex min-w-0 max-w-full items-center gap-1 rounded-full border border-slate-500/30 bg-slate-500/10 px-2 py-0.5 text-[10px] text-slate-400" title={`via VPN · ${vpnName}`}>
              <svg viewBox="0 0 24 24" className="h-2.5 w-2.5 shrink-0" aria-hidden="true">
                <path fill="currentColor" d="M12 1 3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4Z" />
              </svg>
              <span className="truncate">via VPN · {vpnName}</span>
            </span>
          )}
          {isDefault && (
            <span className="rounded-full border border-brand-500/40 bg-brand-500/10 px-2 py-0.5 text-[10px] text-brand-300">
              default
            </span>
          )}
          {hasSecret && (
            <span className="text-slate-400" title="Has stored credential">
              <LockIcon />
            </span>
          )}
          {!isActive && (
            <span className="rounded-full border border-slate-500/40 bg-slate-500/10 px-2 py-0.5 text-slate-400">
              inactive
            </span>
          )}
        </div>
      </div>
      <PermissionGate action="manage_access" organizationId={organizationId}>
      <div className="flex flex-wrap gap-1 sm:shrink-0">
        <button
          className="aist-icon-button border-night-400/60 bg-night-700/60 text-slate-300 text-[11px] px-2.5 py-1.5"
          disabled={isPendingValidate}
          onClick={onValidate}
          title="Validate credentials"
        >
          {isPendingValidate ? (
            <>
              <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 animate-spin" aria-hidden="true">
                <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" opacity="0.25" />
                <path fill="currentColor" opacity="0.75" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4Z" />
              </svg>
              Validating…
            </>
          ) : (
            <>
              <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
                <path fill="currentColor" d="m9 12 2 2 4-4 1.4 1.4-5.4 5.4-3.4-3.4L9 12ZM12 2 4 6v6c0 5.5 3.8 9.9 8 11 4.2-1.1 8-5.5 8-11V6l-8-4Z" />
              </svg>
              Validate
            </>
          )}
        </button>
        {onSynchronize && (
          <button
            className="aist-icon-button border-night-400/60 bg-night-700/60 text-slate-300 text-[11px] px-2.5 py-1.5"
            disabled={isPendingSynchronize}
            onClick={onSynchronize}
            title="Refresh the target catalog from the provider"
          >
            <svg viewBox="0 0 24 24" className={`h-3.5 w-3.5${isPendingSynchronize ? " animate-spin" : ""}`} aria-hidden="true">
              <path fill="currentColor" d="M12 6V3L8 7l4 4V8a4 4 0 1 1-4 4H6a6 6 0 1 0 6-6Z" />
            </svg>
            {isPendingSynchronize ? "Synchronizing…" : "Synchronize"}
          </button>
        )}
        <button
          className="aist-icon-button border-night-400/60 bg-night-700/60 text-slate-300 text-[11px] px-2.5 py-1.5"
          onClick={onEdit}
          title="Edit"
        >
          <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
            <path fill="currentColor" d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25ZM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83Z" />
          </svg>
          Edit
        </button>
        <button
          className="inline-flex items-center gap-1.5 rounded-xl border border-danger-500/50 bg-danger-500/10 px-2.5 py-1.5 text-[11px] font-semibold text-danger-400 transition hover:bg-danger-500/20 disabled:opacity-60"
          disabled={isPendingDelete}
          onClick={onDelete}
          title={destructiveTitle}
        >
          <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
            <path fill="currentColor" d="M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12ZM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4Z" />
          </svg>
          {destructiveLabel}
        </button>
      </div>
      </PermissionGate>
    </div>
  );
}

export function SaveCancelButtons({
  onSave,
  onCancel,
  isPending,
  disabled,
  label,
}: {
  onSave: () => void;
  onCancel: () => void;
  isPending: boolean;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <div className="flex flex-wrap gap-2">
      <button
        className="aist-icon-button border-brand-500/50 bg-brand-500/15 text-brand-100 hover:border-brand-400/70 hover:bg-brand-500/25"
        disabled={isPending || disabled}
        onClick={onSave}
      >
        <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
          <path fill="currentColor" d="M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z" />
        </svg>
        {label ?? "Save"}
      </button>
      <button
        className="aist-icon-button border-night-400/80 bg-night-800/80 text-slate-200"
        disabled={isPending}
        onClick={onCancel}
      >
        Cancel
      </button>
    </div>
  );
}
