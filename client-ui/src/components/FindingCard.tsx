import type { Finding } from "../types";
import { countReproductionSteps } from "../lib/dastNarrative";
import { formatDateForUI } from "../lib/dateDisplay";
import { formatProjectVersionText } from "../lib/projectVersion";
import { findingStatusBadgeClass, severityBadgeClass } from "../lib/badgeStyles";
import { getFindingStatusBadges, isDastFinding } from "../lib/findingStatus";
import { PROVIDER_ICON_PATHS } from "../lib/providerIcons";
import AiVerdictBadge from "./AiVerdictBadge";
import FindingSnippetPreview from "./FindingSnippetPreview";

function EndpointsPreview({ endpoints }: { endpoints: string[] }) {
  if (endpoints.length === 0) {
    return <p className="mt-3 text-xs text-slate-500">No endpoints reported.</p>;
  }
  return (
    <div className="mt-3 flex flex-col gap-1 text-xs text-slate-400">
      {endpoints.slice(0, 3).map((endpoint) => (
        <span key={endpoint} className="truncate" title={endpoint}>
          {endpoint}
        </span>
      ))}
      {endpoints.length > 3 ? (
        <span className="text-slate-500">+{endpoints.length - 3} more endpoint(s)</span>
      ) : null}
    </div>
  );
}

type FindingCardProps = {
  finding: Finding;
  onSelect: (finding: Finding) => void;
  isOpen?: boolean;
  expandedContent?: React.ReactNode;
  onSelectProjectVersion?: (projectVersion: string) => void;
  onSelectProject?: (projectId: number) => void;
  onSelectFile?: (filePath: string) => void;
  showBulkSelection?: boolean;
  selectedForBulk?: boolean;
  onToggleBulkSelection?: (findingId: number, checked: boolean) => void;
  bulkLocked?: boolean;
};

export default function FindingCard({
  finding,
  onSelect,
  isOpen = false,
  expandedContent,
  onSelectProjectVersion,
  onSelectProject,
  onSelectFile,
  showBulkSelection = false,
  selectedForBulk = false,
  onToggleBulkSelection,
  bulkLocked = false,
}: FindingCardProps) {
  const createdLabel = formatDateForUI(finding.createdAt) ?? formatDateForUI(finding.date);
  const isDast = isDastFinding(finding);
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
          {showBulkSelection ? (
            <label
              className={[
                "inline-flex items-center gap-2 rounded-xl border px-2.5 py-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] transition",
                selectedForBulk
                  ? "border-brand-500/60 bg-brand-500/10 text-brand-200"
                  : "border-night-500 bg-night-800 text-slate-300 hover:border-brand-600/50",
                bulkLocked ? "opacity-60" : "",
              ].join(" ")}
              onClick={(event) => event.stopPropagation()}
            >
              <input
                type="checkbox"
                className="peer sr-only"
                checked={selectedForBulk}
                onChange={(event) => onToggleBulkSelection?.(finding.id, event.target.checked)}
                disabled={bulkLocked}
                aria-label={`Select finding ${finding.id}`}
              />
              <span
                className={[
                  "inline-flex h-4 w-4 items-center justify-center rounded-md border transition",
                  selectedForBulk
                    ? "border-brand-500 bg-brand-500/90 text-night-900 shadow-[0_0_0_1px_rgba(77,212,255,0.28)]"
                    : "border-night-400/80 bg-night-700 text-transparent",
                ].join(" ")}
                aria-hidden="true"
              >
                <svg viewBox="0 0 12 12" className="h-3 w-3">
                  <path fill="currentColor" d="M4.8 9.2 1.6 6l1.1-1.1 2.1 2.1 4.5-4.5 1.1 1.1-5.6 5.6Z" />
                </svg>
              </span>
              <span>{selectedForBulk ? "Selected" : "Select"}</span>
            </label>
          ) : null}
          <span
            className={[
              "rounded-full border px-3 py-1 font-semibold uppercase tracking-wide",
              severityBadgeClass(finding.severity),
            ].join(" ")}
          >
            {finding.severity}
          </span>
          <AiVerdictBadge verdict={finding.aiVerdict} />
          {isDast ? (
            <span className="rounded-full border border-rose-500/40 bg-rose-500/10 px-3 py-1 text-xs font-medium uppercase tracking-wide text-rose-300">
              DAST
            </span>
          ) : null}
          {finding.isRegression ? (
            <span className="rounded-full border border-orange-500/40 bg-orange-500/10 px-3 py-1 text-xs font-medium text-orange-200">
              Regression
            </span>
          ) : null}
          {getFindingStatusBadges(finding).map((status) => (
            <span key={status} className={`rounded-full border px-3 py-1 text-xs ${findingStatusBadgeClass(status)}`}>
              {status}
            </span>
          ))}
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
        {isDast ? (
          <span className="inline-flex items-center gap-1">
            {finding.affectedEndpoints?.length ?? 0} endpoint
            {(finding.affectedEndpoints?.length ?? 0) === 1 ? "" : "s"} ·{" "}
            {countReproductionSteps(finding.stepsToReproduce)} step
            {countReproductionSteps(finding.stepsToReproduce) === 1 ? "" : "s"}
          </span>
        ) : (
          <>
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
            {finding.line && finding.line > 0 ? <span>Line {finding.line}</span> : null}
          </>
        )}
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
        {finding.projectId ? (
          <button
            type="button"
            className="aist-clickable-text inline-flex items-center gap-1"
            onClick={(event) => {
              event.stopPropagation();
              onSelectProject?.(finding.projectId!);
            }}
            title={finding.product || String(finding.projectId)}
          >
            <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 3 4 7.5 12 12l8-4.5L12 3Z" />
              <path d="M4 7.5V16.5L12 21" />
              <path d="M20 7.5V16.5L12 21" />
            </svg>
            <span>Project: {finding.product || `Project ${finding.projectId}`}</span>
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
        {finding.lastStatusUpdate ? (
          <span className="inline-flex items-center gap-1">
            <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
              <path fill="currentColor" d="M12 4V1L8 5l4 4V6a6 6 0 1 1-6 6H4a8 8 0 1 0 8-8Z" />
            </svg>
            Updated {new Date(finding.lastStatusUpdate).toLocaleDateString()}
          </span>
        ) : null}
        {finding.workItems?.map((wi) => (
          <a
            key={wi.id}
            href={wi.externalUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="aist-clickable-text inline-flex items-center gap-1"
            onClick={(event) => event.stopPropagation()}
            title={wi.title || wi.externalKey || wi.externalUrl}
          >
            <svg viewBox="0 0 24 24" className="h-3 w-3 shrink-0" aria-hidden="true">
              <path
                fill="currentColor"
                d={PROVIDER_ICON_PATHS[wi.providerType ?? ""] ?? PROVIDER_ICON_PATHS.GENERIC}
              />
            </svg>
            {wi.externalKey || wi.providerName || "Issue"}
          </a>
        ))}
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
        ) : isDast ? (
          <EndpointsPreview endpoints={finding.affectedEndpoints ?? []} />
        ) : (
          <FindingSnippetPreview
            filePath={finding.filePath}
            sourceFileLink={finding.sourceFileLink}
            line={finding.line}
          />
        )}
      </div>
    </article>
  );
}
