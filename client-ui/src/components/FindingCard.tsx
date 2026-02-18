import type { Finding } from "../types";
import { formatProjectVersionText } from "../lib/projectVersion";
import AiVerdictBadge from "./AiVerdictBadge";
import FindingSnippetPreview from "./FindingSnippetPreview";

type FindingCardProps = {
  finding: Finding;
  projectId?: number;
  projectVersionId?: number;
  onSelect: (finding: Finding) => void;
  isOpen?: boolean;
  expandedContent?: React.ReactNode;
  onSelectProjectVersion?: (projectVersion: string) => void;
  onSelectFile?: (filePath: string) => void;
};

const severityStyles: Record<Finding["severity"], string> = {
  Critical: "border-danger-500/50 text-danger-500 bg-danger-500/10",
  High: "border-danger-500/30 text-danger-500/80 bg-danger-500/10",
  Medium: "border-amber-400/40 text-amber-400 bg-amber-400/10",
  Low: "border-slate-500/40 text-slate-300 bg-slate-500/10",
  Info: "border-slate-500/40 text-slate-300 bg-slate-500/10",
};

export default function FindingCard({
  finding,
  projectId,
  projectVersionId,
  onSelect,
  isOpen = false,
  expandedContent,
  onSelectProjectVersion,
  onSelectFile,
}: FindingCardProps) {
  const createdLabel = finding.createdAt
    ? new Date(finding.createdAt).toLocaleString()
    : finding.date
      ? new Date(finding.date).toLocaleString()
      : null;
  return (
    <article
      className={[
        "min-w-0 p-5 overflow-hidden aist-card aist-card--interactive",
        isOpen ? "aist-card--expanded" : "",
      ].join(" ")}
      role="button"
      tabIndex={0}
      aria-expanded={isOpen}
      onClick={() => onSelect(finding)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect(finding);
        }
      }}
    >
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-400">
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={[
              "rounded-full border px-3 py-1 font-semibold uppercase tracking-wide",
              severityStyles[finding.severity],
            ].join(" ")}
          >
            {finding.severity}
          </span>
          <AiVerdictBadge verdict={finding.aiVerdict} />
          {finding.isMitigated ? (
            <span className="rounded-full border border-emerald-400/40 bg-emerald-400/10 px-3 py-1 text-xs text-emerald-300">
              Mitigated
            </span>
          ) : null}
          {finding.riskAccepted ? (
            <span className="rounded-full border border-amber-400/40 bg-amber-400/10 px-3 py-1 text-xs text-amber-300">
              Risk Accepted
            </span>
          ) : null}
          {finding.falsePositive ? (
            <span className="rounded-full border border-purple-400/40 bg-purple-400/10 px-3 py-1 text-xs text-purple-300">
              False Positive
            </span>
          ) : null}
          {finding.outOfScope ? (
            <span className="rounded-full border border-slate-400/40 bg-slate-400/10 px-3 py-1 text-xs text-slate-300">
              Out of Scope
            </span>
          ) : null}
          {finding.duplicate ? (
            <span className="rounded-full border border-slate-400/40 bg-slate-400/10 px-3 py-1 text-xs text-slate-300">
              Duplicate
            </span>
          ) : null}
          {!finding.isMitigated &&
          !finding.riskAccepted &&
          !finding.falsePositive &&
          !finding.outOfScope &&
          !finding.duplicate ? (
            <span className="rounded-full border border-night-500 bg-night-900 px-3 py-1 text-xs text-slate-200">
              {finding.active ? "Active" : "Non-Active"}
            </span>
          ) : null}
        </div>
        <span
          className={[
            "inline-flex h-6 w-6 items-center justify-center rounded-full border border-night-500 bg-night-800 text-slate-300 transition-transform",
            isOpen ? "rotate-180" : "",
          ].join(" ")}
          aria-hidden="true"
        >
          <svg viewBox="0 0 24 24" className="h-4 w-4">
            <path
              fill="currentColor"
              d="M7 10l5 5 5-5H7Z"
            />
          </svg>
        </span>
      </div>
      <div
        className="mt-3 text-base font-semibold text-white line-clamp-2"
        title={finding.title}
      >
        {finding.title}
      </div>
      <div className="mt-2 flex flex-wrap gap-3 text-xs text-slate-400">
        <button
          type="button"
          className="aist-clickable-text inline-flex max-w-full items-center gap-1 truncate text-left"
          title={finding.filePath}
          onClick={(event) => {
            event.stopPropagation();
            onSelectFile?.(finding.filePath);
          }}
        >
          <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 shrink-0" aria-hidden="true">
            <path
              fill="currentColor"
              d="M6 2h8l4 4v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2Zm7 1.5V7h3.5L13 3.5ZM6 4v16h10V8h-4a1 1 0 0 1-1-1V4H6Z"
            />
          </svg>
          <span className="truncate">File: {finding.filePath}</span>
        </button>
        <span>Line {finding.line}</span>
        {finding.projectVersion ? (
          <button
            type="button"
            className="aist-clickable-text inline-flex items-center gap-1"
            onClick={(event) => {
              event.stopPropagation();
              onSelectProjectVersion?.(finding.projectVersion);
            }}
            title={finding.projectVersion}
          >
            <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
              <path
                fill="currentColor"
                d="M7 6a3 3 0 1 1 2.83 4H9v4h1a3 3 0 1 1 0 2H9a2 2 0 0 1-2-2v-4a3 3 0 0 1 0-4Z"
              />
            </svg>
            {formatProjectVersionText(finding.projectVersion, finding.projectVersionType)}
          </button>
        ) : null}
        {createdLabel ? (
          <span className="inline-flex items-center gap-1">
            <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
              <path fill="currentColor" d="M7 2h2v2h6V2h2v2h3v18H4V4h3V2Zm11 8H6v10h12V10Zm0-4H6v2h12V6Z" />
            </svg>
            Created {createdLabel}
          </span>
        ) : null}
      </div>
      <div className="mt-3">
        {isOpen ? (
          <div
            className="panel-collapse"
            data-state="open"
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => event.stopPropagation()}
          >
            <div className="panel-collapse-inner">
              {expandedContent}
            </div>
          </div>
        ) : (
          <FindingSnippetPreview
            projectId={projectId}
            projectVersionId={projectVersionId}
            filePath={finding.filePath}
            sourceFileLink={finding.sourceFileLink}
            line={finding.line}
          />
        )}
      </div>
      <div className="mt-4 text-xs text-slate-400">
        {isOpen ? "Click to collapse." : "Click to view details."}
      </div>
    </article>
  );
}
