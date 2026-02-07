import { Link } from "react-router-dom";

import type { Finding } from "../types";
import { useUpdateFindingStatus } from "../lib/mutations";
import { useToast } from "./ToastProvider";

type FindingCardProps = {
  finding: Finding;
  onSelect: (finding: Finding) => void;
};

const severityStyles: Record<Finding["severity"], string> = {
  Critical: "border-danger-500/50 text-danger-500 bg-danger-500/10",
  High: "border-danger-500/30 text-danger-500/80 bg-danger-500/10",
  Medium: "border-amber-400/40 text-amber-400 bg-amber-400/10",
  Low: "border-slate-500/40 text-slate-300 bg-slate-500/10",
  Info: "border-slate-500/40 text-slate-300 bg-slate-500/10",
};

const verdictLabel = {
  true_positive: "AI: True Positive",
  false_positive: "AI: False Positive",
  uncertain: "AI: Uncertain",
};

export default function FindingCard({ finding, onSelect }: FindingCardProps) {
  const updateStatus = useUpdateFindingStatus();
  const toast = useToast();
  return (
    <article
      className="rounded-2xl border border-night-500 bg-night-700 p-5 shadow-panel hover:border-brand-600/70 transition"
      onClick={() => onSelect(finding)}
    >
      <div className="flex items-center justify-between text-xs text-slate-400">
        <span
          className={[
            "rounded-full border px-3 py-1 font-semibold uppercase tracking-wide",
            severityStyles[finding.severity],
          ].join(" ")}
        >
          {finding.severity}
        </span>
        <span>{finding.active ? "Enabled" : "Disabled"}</span>
      </div>
      <div className="mt-3 text-base font-semibold text-white">{finding.title}</div>
      <div className="mt-2 flex flex-wrap gap-3 text-xs text-slate-400">
        <span>{finding.filePath}</span>
        <span>Line {finding.line}</span>
        {finding.aiVerdict ? <span>{verdictLabel[finding.aiVerdict]}</span> : null}
      </div>
      <div className="mt-3 rounded-xl border border-night-500 bg-night-900 px-4 py-3 font-mono text-xs text-slate-200">
        {finding.snippetPreview ?? "Snippet preview unavailable"}
      </div>
      <div className="mt-4 flex gap-2">
        <button
          className="rounded-xl border border-night-500 bg-transparent px-3 py-2 text-xs text-white"
          onClick={(event) => {
            event.stopPropagation();
            updateStatus.mutate(
              { id: finding.id, active: !finding.active },
              {
                onSuccess: () => {
                  toast.push(
                    finding.active ? "Finding disabled." : "Finding enabled.",
                    "success",
                  );
                },
                onError: (error) => {
                  const message = error instanceof Error ? error.message : String(error);
                  toast.push(`Action failed: ${message}`, "error");
                },
              },
            );
          }}
        >
          {finding.active ? "Disable" : "Enable"}
        </button>
        <button
          className="rounded-xl border border-night-500 bg-transparent px-3 py-2 text-xs text-white"
          onClick={(event) => {
            event.stopPropagation();
            onSelect(finding);
          }}
        >
          Comment
        </button>
        <Link
          to={`/finding/${finding.id}`}
          className="rounded-xl bg-brand-500 px-3 py-2 text-xs font-semibold text-night-900"
          onClick={(event) => event.stopPropagation()}
        >
          Open Detail
        </Link>
      </div>
    </article>
  );
}
