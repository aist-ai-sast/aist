import { Suspense, lazy } from "react";
import type { AIResponse, Finding } from "../types";
import { type FindingCloseReason, useExportFinding } from "../lib/mutations";
import { useToast } from "./ToastProvider";
import FindingStatusActions from "./FindingStatusActions";
import FindingSeverityControl from "./FindingSeverityControl";
import { Link } from "react-router-dom";
import { getRoute } from "../lib/routes";

const FindingDetailTabs = lazy(() => import("./FindingDetailTabs"));

type DetailPanelProps = {
  finding?: Finding;
  permissionProductId?: number;
  permissionOrganizationId?: number;
  aiResponse?: AIResponse | null;
  embedded?: boolean;
  selectedTags?: string[];
  onToggleTag?: (tag: string) => void;
  selectedCwe?: string;
  onToggleCwe?: (cwe: string) => void;
  onCloseApplied?: (findingId: number, reason: FindingCloseReason) => void;
  onReopened?: (findingId: number) => void;
  onSeverityChanged?: (findingId: number, severity: "Critical" | "High" | "Medium" | "Low" | "Info") => void;
  isStatusEditLocked?: boolean;
};

export default function DetailPanel({
  finding,
  permissionProductId,
  permissionOrganizationId,
  aiResponse,
  embedded = false,
  selectedTags,
  onToggleTag,
  selectedCwe,
  onToggleCwe,
  onCloseApplied,
  onReopened,
  onSeverityChanged,
  isStatusEditLocked = false,
}: DetailPanelProps) {
  const exportFinding = useExportFinding();
  const toast = useToast();
  if (!finding) {
    if (embedded) return null;
    return (
      <aside className="p-5 text-sm text-slate-400 aist-card">
        Select a finding to view detail.
      </aside>
    );
  }

  const exportDisabled = !finding?.id;
  const headerAction = (
    <button
      className="aist-icon-button h-10 disabled:opacity-50"
      onClick={() => {
        if (!finding?.id) {
          toast.push("Finding export is unavailable.", "error");
          return;
        }
        exportFinding.mutate(
          { findingId: finding.id },
          {
            onSuccess: () => {
              toast.push("Export started.", "success");
            },
            onError: (error) => {
              const message = error instanceof Error ? error.message : String(error);
              toast.push(`Export failed: ${message}`, "error");
            },
          },
        );
      }}
      disabled={exportDisabled}
      title="Export finding"
    >
      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
        <path
          fill="currentColor"
          d="M12 3l4 4h-3v6h-2V7H8l4-4Zm-7 12h14v4a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-4Zm3 2v2h8v-2H8Z"
        />
      </svg>
      Export
    </button>
  );

  const content = (
    <>
      {embedded ? null : (
        <>
          <div className="flex items-center justify-between gap-3">
            <div className="text-xs uppercase tracking-[0.2em] text-slate-400">
              Finding Detail
            </div>
            {headerAction}
          </div>
          <h2 className="mt-3 text-lg font-semibold text-white line-clamp-2" title={finding.title}>
            {finding.title}
          </h2>
        </>
      )}
      {embedded ? (
        <div className="flex items-center justify-between gap-3">
          <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Detail</div>
          {headerAction}
        </div>
      ) : null}
      <div className="mt-3" />
      <div className="mt-4">
        <Suspense
          fallback={(
            <div className="rounded-xl border border-night-500 bg-night-900 px-4 py-3 text-sm text-slate-400">
              Loading detail...
            </div>
          )}
        >
          <FindingDetailTabs
            finding={finding}
            permissionProductId={permissionProductId}
            permissionOrganizationId={permissionOrganizationId}
            aiResponse={aiResponse}
            embedded={embedded}
            selectedTags={selectedTags}
            onToggleTag={onToggleTag}
            selectedCwe={selectedCwe}
            onToggleCwe={onToggleCwe}
          />
        </Suspense>
      </div>
      <div className="mt-4 flex flex-wrap items-end gap-3">
        <FindingSeverityControl
          finding={finding}
          permissionProductId={permissionProductId}
          permissionOrganizationId={permissionOrganizationId}
          onChanged={(severity) => onSeverityChanged?.(finding.id, severity)}
          isLocked={isStatusEditLocked}
        />
        <FindingStatusActions
          finding={finding}
          permissionProductId={permissionProductId}
          permissionOrganizationId={permissionOrganizationId}
          onApplied={(reason) => onCloseApplied?.(finding.id, reason)}
          onReopened={() => onReopened?.(finding.id)}
          isLocked={isStatusEditLocked}
        />
      </div>
      <Link
        to={getRoute("ui_finding_detail_path", { id: finding.id })}
        className="mt-4 inline-flex text-sm text-brand-500"
      >
        Open full detail →
      </Link>
    </>
  );

  if (embedded) {
    return <div className="space-y-4">{content}</div>;
  }

  return <aside className="p-5 aist-card">{content}</aside>;
}
