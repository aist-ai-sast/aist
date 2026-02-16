import type { AIResponse, Finding } from "../types";
import { useExportAiResults } from "../lib/mutations";
import { useToast } from "./ToastProvider";
import FindingStatusActions from "./FindingStatusActions";
import FindingDetailTabs from "./FindingDetailTabs";
import { Link } from "react-router-dom";
import { getRoute } from "../lib/routes";

type DetailPanelProps = {
  finding?: Finding;
  aiResponse?: AIResponse | null;
  pipelineId?: string;
  embedded?: boolean;
  selectedTags?: string[];
  onToggleTag?: (tag: string) => void;
  selectedCwe?: string;
  onToggleCwe?: (cwe: string) => void;
  onCloseApplied?: (findingId: number, reason: "mitigated" | "false_positive" | "out_of_scope" | "duplicate") => void;
  onReopened?: (findingId: number) => void;
};

export default function DetailPanel({
  finding,
  aiResponse,
  pipelineId,
  embedded = false,
  selectedTags,
  onToggleTag,
  selectedCwe,
  onToggleCwe,
  onCloseApplied,
  onReopened,
}: DetailPanelProps) {
  const exportAi = useExportAiResults();
  const toast = useToast();
  if (!finding) {
    if (embedded) return null;
    return (
      <aside className="p-5 text-sm text-slate-400 aist-card">
        Select a finding to view detail.
      </aside>
    );
  }

  const resolvedPipelineId = pipelineId ?? aiResponse?.pipelineId;
  const exportDisabled = !resolvedPipelineId || !aiResponse;
  const headerAction = (
    <button
      className="aist-icon-button h-10 disabled:opacity-50"
      onClick={() => {
        if (exportDisabled) {
          toast.push("AI results are not available for export.", "error");
          return;
        }
        exportAi.mutate(
          { pipelineId: resolvedPipelineId! },
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
      title="Export AI results"
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
        <FindingDetailTabs
          finding={finding}
          aiResponse={aiResponse}
          embedded={embedded}
          selectedTags={selectedTags}
          onToggleTag={onToggleTag}
          selectedCwe={selectedCwe}
          onToggleCwe={onToggleCwe}
        />
      </div>
      <div className="mt-4 flex items-center justify-between gap-3">
        <FindingStatusActions
          finding={finding}
          onApplied={(reason) => onCloseApplied?.(finding.id, reason)}
          onReopened={() => onReopened?.(finding.id)}
        />
      </div>
      <Link to={getRoute("ui_finding_detail_path", { id: finding.id })} className="mt-4 inline-flex text-sm text-brand-500">
        Open full detail →
      </Link>
    </>
  );

  if (embedded) {
    return <div className="space-y-4">{content}</div>;
  }

  return <aside className="p-5 aist-card">{content}</aside>;
}
