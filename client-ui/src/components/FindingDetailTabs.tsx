import { useState } from "react";

import type { AIResponse, Finding, FindingTimelineEvent } from "../types";
import CodeSnippet from "./CodeSnippet";
import CweTooltip from "./CweTooltip";
import DescriptionBlock from "./DescriptionBlock";
import StepsToReproduce from "./StepsToReproduce";
import { inferMarkdownStructure } from "../lib/dastNarrative";
import { isDastFinding } from "../lib/findingStatus";
import { useAddFindingNote } from "../lib/mutations";
import { useFinding, useFindingNotes, useFindingTimeline } from "../lib/queries";
import PermissionGate from "./PermissionGate";
import { useToast } from "./ToastProvider";
import { ACCENT_SELECTED_CLASS } from "../lib/uiClasses";
import WorkItemsPanel from "./WorkItemsPanel";
import AiFixPanel from "./AiFixPanel";

type FindingDetailTabsProps = {
  finding: Finding;
  permissionProductId?: number;
  aiResponse?: AIResponse | null;
  embedded?: boolean;
  selectedTags?: string[];
  onToggleTag?: (tag: string) => void;
  selectedCwe?: string;
  onToggleCwe?: (cwe: string) => void;
};

type TabId = "overview" | "ai" | "code" | "notes" | "history" | "work_items";

function OverviewIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3.5" y="3.5" width="17" height="17" rx="2.5" />
      <line x1="7" y1="8.5" x2="17" y2="8.5" />
      <line x1="7" y1="12" x2="17" y2="12" />
      <line x1="7" y1="15.5" x2="13" y2="15.5" />
    </svg>
  );
}
function AiIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}
function CodeEvidenceIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="8 6 3 12 8 18" />
      <polyline points="16 6 21 12 16 18" />
    </svg>
  );
}
function NotesIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M20 15a2 2 0 0 1-2 2H8l-4 4V5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z" />
    </svg>
  );
}
function HistoryIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <polyline points="12 7 12 12 15.5 14" />
    </svg>
  );
}
function WorkItemsIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3.5" y="5.5" width="17" height="15" rx="2" />
      <line x1="3.5" y1="10" x2="20.5" y2="10" />
      <line x1="8" y1="3" x2="8" y2="7" />
      <line x1="16" y1="3" x2="16" y2="7" />
    </svg>
  );
}

const tabs: Array<{ id: TabId; label: string; Icon: () => JSX.Element }> = [
  { id: "overview", label: "Overview", Icon: OverviewIcon },
  { id: "ai", label: "AI Assessment", Icon: AiIcon },
  { id: "code", label: "Code", Icon: CodeEvidenceIcon },
  { id: "notes", label: "Notes", Icon: NotesIcon },
  { id: "history", label: "History", Icon: HistoryIcon },
  { id: "work_items", label: "Work Items", Icon: WorkItemsIcon },
];

const verdictMeta: Record<NonNullable<AIResponse["verdict"]>, { label: string; className: string }> = {
  true_positive: {
    label: "True Positive",
    className: "border-danger-500/40 bg-danger-500/10 text-danger-200",
  },
  false_positive: {
    label: "False Positive",
    className: "border-emerald-500/40 bg-emerald-500/10 text-emerald-200",
  },
  uncertain: {
    label: "Uncertain",
    className: "border-amber-400/40 bg-amber-400/10 text-amber-200",
  },
};

const EMBEDDED_HISTORY_ITEMS_LIMIT = 8;
const HISTORY_BADGE_LABEL: Record<FindingTimelineEvent["eventType"], string> = {
  finding_created: "Created",
  finding_processed: "Processed",
  finding_note_added: "Comment",
};

function formatScore(value?: number) {
  if (typeof value !== "number" || Number.isNaN(value)) return "n/a";
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

type RiskBadge = { value: string; label: string; className: string };

function formatEpss(value?: number): RiskBadge | null {
  if (typeof value !== "number" || Number.isNaN(value)) return null;
  const pct = Math.round(value * 100);
  if (pct >= 20) return { value: `${pct}%`, label: "High",   className: "border-danger-500/40 bg-danger-500/10 text-danger-200" };
  if (pct >= 5)  return { value: `${pct}%`, label: "Medium", className: "border-amber-400/40 bg-amber-400/10 text-amber-200" };
  return           { value: `${pct}%`, label: "Low",    className: "border-emerald-500/40 bg-emerald-500/10 text-emerald-200" };
}

function formatCvss(value?: number): RiskBadge | null {
  if (typeof value !== "number" || Number.isNaN(value)) return null;
  const display = Number.isInteger(value) ? String(value) : value.toFixed(1);
  if (value >= 7) return { value: display, label: "High",   className: "border-danger-500/40 bg-danger-500/10 text-danger-200" };
  if (value >= 4) return { value: display, label: "Medium", className: "border-amber-400/40 bg-amber-400/10 text-amber-200" };
  return            { value: display, label: "Low",    className: "border-emerald-500/40 bg-emerald-500/10 text-emerald-200" };
}

function confidenceMeta(uncertaintyLevel?: number): { text: string; className: string } {
  if (typeof uncertaintyLevel !== "number" || Number.isNaN(uncertaintyLevel)) {
    return { text: "n/a", className: "border-night-500 bg-night-800 text-slate-300" };
  }
  const pct = Math.round((1 - Math.max(0, Math.min(1, uncertaintyLevel))) * 100);
  if (pct >= 70) return { text: `${pct}% confidence`, className: "border-emerald-500/40 bg-emerald-500/10 text-emerald-200" };
  if (pct >= 50) return { text: `${pct}% confidence`, className: "border-night-500 bg-night-800 text-slate-300" };
  return           { text: `${pct}% confidence`, className: "border-amber-400/40 bg-amber-400/10 text-amber-200" };
}

function refLabel(url: string): string {
  try {
    const hostname = new URL(url).hostname;
    return hostname.startsWith("www.") ? hostname.slice(4) : hostname;
  } catch {
    return url;
  }
}

function FindingReferences({ value }: { value: string }) {
  const references = value
    .split("\n")
    .map((reference) => reference.trim())
    .filter(Boolean);
  return (
    <ul className="space-y-1">
      {references.map((reference, index) => {
        try {
          const url = new URL(reference);
          if (url.protocol === "http:" || url.protocol === "https:") {
            return (
              <li key={`${index}-${reference}`}>
                <a
                  href={url.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="break-all text-brand-200 hover:text-brand-100"
                >
                  {reference}
                </a>
              </li>
            );
          }
        } catch {
          // Plain reference labels fall through to text rendering.
        }
        return (
          <li key={`${index}-${reference}`} className="break-all">
            {reference}
          </li>
        );
      })}
    </ul>
  );
}

export default function FindingDetailTabs({
  finding,
  permissionProductId,
  aiResponse,
  embedded = false,
  selectedTags = [],
  onToggleTag,
  selectedCwe,
  onToggleCwe,
}: FindingDetailTabsProps) {
  const [tab, setTab] = useState<TabId>("overview");
  const [note, setNote] = useState("");
  const toast = useToast();
  const notesQuery = useFindingNotes(finding.id);
  const addNote = useAddFindingNote();
  const noteItems = notesQuery.data ?? [];
  const displayNotes = embedded ? noteItems.slice(0, 5) : noteItems;
  const detailQuery = useFinding(finding.id);
  const timelineQuery = useFindingTimeline(finding.id);
  const resolvedTags =
    finding.tags && finding.tags.length > 0
      ? finding.tags
      : detailQuery.data?.tags ?? [];
  const tagsLoading = !finding.tags?.length && detailQuery.isLoading;

  const isDast = isDastFinding(finding);

  return (
    <div>
      <div className="flex flex-wrap gap-2">
        {tabs.map((item) => (
          <button
            key={item.id}
            type="button"
            className={[
              "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs",
              tab === item.id
                ? ACCENT_SELECTED_CLASS
                : "border-night-500 bg-night-900 text-slate-300",
            ].join(" ")}
            onClick={() => setTab(item.id)}
          >
            <item.Icon />
            {item.id === "code" && isDast ? "Evidence" : item.label}
          </button>
        ))}
      </div>

      {tab === "overview" ? (
        <div className="mt-4 space-y-4">
          {resolvedTags.length ? (
            <div>
              <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Tags</div>
              <div className="mt-2 flex flex-wrap gap-2">
                {resolvedTags.map((tag) => (
                  <button
                    key={tag}
                    type="button"
                    className={[
                      "rounded-full border px-3 py-1 text-xs transition",
                      selectedTags.includes(tag)
                        ? ACCENT_SELECTED_CLASS
                        : "border-night-500 bg-night-900 text-slate-200 hover:border-brand-600/40",
                    ].join(" ")}
                    onClick={() => onToggleTag?.(tag)}
                  >
                    {tag}
                  </button>
                ))}
              </div>
            </div>
          ) : tagsLoading ? (
            <div className="text-xs text-slate-500">Loading tags...</div>
          ) : (
            <div className="text-xs text-slate-500">No tags available.</div>
          )}

          <div>
            <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Description</div>
            <div className="mt-2 rounded-xl border border-night-500 bg-night-900 px-4 py-3">
              <DescriptionBlock value={finding.description} />
            </div>
          </div>

          {isDastFinding(finding) ? (
            <>
              {finding.impact ? (
                <div>
                  <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Impact</div>
                  <div className="mt-2 rounded-xl border border-night-500 bg-night-900 px-4 py-3">
                    <DescriptionBlock value={inferMarkdownStructure(finding.impact)} />
                  </div>
                </div>
              ) : null}

              {finding.mitigation ? (
                <div>
                  <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Mitigation</div>
                  <div className="mt-2 rounded-xl border border-night-500 bg-night-900 px-4 py-3">
                    <DescriptionBlock value={inferMarkdownStructure(finding.mitigation)} />
                  </div>
                </div>
              ) : null}

              <div>
                <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Affected Endpoints</div>
                <div className="mt-2 rounded-xl border border-night-500 bg-night-900 px-4 py-3 text-sm text-slate-200">
                  {finding.affectedEndpoints?.length ? (
                    <ul className="space-y-1">
                      {finding.affectedEndpoints.map((endpoint) => (
                        <li key={endpoint} className="break-all font-mono text-xs">
                          {endpoint}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <span className="text-slate-500">No endpoints reported.</span>
                  )}
                </div>
              </div>

              {finding.param || finding.payload || finding.cvssv3 ? (
                <div>
                  <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Attack Vector</div>
                  <div className="mt-2 space-y-2 rounded-xl border border-night-500 bg-night-900 px-4 py-3 text-sm text-slate-200">
                    {finding.param ? (
                      <div>
                        <span className="text-xs text-slate-400">Param: </span>
                        <span className="font-mono text-xs">{finding.param}</span>
                      </div>
                    ) : null}
                    {finding.payload ? (
                      <div>
                        <span className="text-xs text-slate-400">Payload: </span>
                        <span className="break-all font-mono text-xs">{finding.payload}</span>
                      </div>
                    ) : null}
                    {finding.cvssv3 ? (
                      <div>
                        <span className="text-xs text-slate-400">CVSS: </span>
                        <span className="font-mono text-xs">
                          {finding.cvssv3}
                          {typeof finding.cvssv3Score === "number" ? ` (${finding.cvssv3Score})` : ""}
                        </span>
                      </div>
                    ) : null}
                  </div>
                </div>
              ) : null}

              {finding.references ? (
                <div>
                  <div className="text-xs uppercase tracking-[0.2em] text-slate-400">References</div>
                  <div className="mt-2 rounded-xl border border-night-500 bg-night-900 px-4 py-3 text-sm text-slate-200">
                    <FindingReferences value={finding.references} />
                  </div>
                </div>
              ) : null}

            </>
          ) : null}

          {finding.cwe && embedded ? (
            <div>
              <div className="text-xs uppercase tracking-[0.2em] text-slate-400">CWE</div>
              <div className="mt-2">
                <button
                  type="button"
                  className={[
                    "rounded-full border px-3 py-1 text-xs transition",
                    selectedCwe === String(finding.cwe)
                      ? ACCENT_SELECTED_CLASS
                      : "border-night-500 bg-night-900 text-slate-200 hover:border-brand-600/40",
                  ].join(" ")}
                  onClick={() => onToggleCwe?.(String(finding.cwe))}
                >
                  <CweTooltip cwe={finding.cwe} />
                </button>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {tab === "ai" ? (
        <div className="mt-4 space-y-4">
          {aiResponse ? (
            <div className="space-y-4 rounded-2xl border border-night-500 bg-night-900/80 p-4 text-sm text-slate-200">
              <div className="flex flex-wrap items-center gap-2">
                {aiResponse.verdict ? (
                  <span
                    className={[
                      "rounded-full border px-3 py-1 text-xs font-medium",
                      verdictMeta[aiResponse.verdict].className,
                    ].join(" ")}
                  >
                    {verdictMeta[aiResponse.verdict].label}
                  </span>
                ) : null}
                {(() => {
                  const cm = confidenceMeta(aiResponse.uncertaintyLevel);
                  return (
                    <span className={`rounded-full border px-3 py-1 text-xs font-medium ${cm.className}`}>
                      {cm.text}
                    </span>
                  );
                })()}
              </div>

              {typeof aiResponse.uncertaintyLevel === "number" && aiResponse.uncertaintyLevel > 0.5 ? (
                <div className="flex items-center gap-2 rounded-xl border border-amber-400/30 bg-amber-400/5 px-3 py-2 text-xs text-amber-200">
                  <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                    <line x1="12" y1="9" x2="12" y2="13" />
                    <line x1="12" y1="17" x2="12.01" y2="17" />
                  </svg>
                  Low confidence — manual review recommended
                </div>
              ) : null}

              {aiResponse.title ? (
                <div className="text-base font-semibold text-white">{aiResponse.title}</div>
              ) : null}

              <div>
                <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Summary</div>
                <div className="mt-2 rounded-xl border border-night-500 bg-night-800/70 px-4 py-3 leading-relaxed text-slate-200">
                  <DescriptionBlock value={aiResponse.reasoning} />
                </div>
              </div>

              <div>
                <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Risk Signals</div>
                <div className="mt-2 grid gap-2 sm:grid-cols-3">
                  {([
                    { label: "Exploit Probability", badge: formatEpss(aiResponse.epssScore), sub: "next 30 days" },
                    { label: "Impact",              badge: formatCvss(aiResponse.impactScore),        sub: "CVSS subscore" },
                    { label: "Exploitability",      badge: formatCvss(aiResponse.exploitabilityScore), sub: "CVSS subscore" },
                  ] as const).map(({ label, badge, sub }) => (
                    <div key={label} className="rounded-xl border border-night-500 bg-night-800 px-3 py-2 text-xs">
                      <div className="text-slate-400">{label}</div>
                      {badge ? (
                        <div className="mt-1 flex items-center gap-1.5">
                          <span className="text-sm font-semibold text-white">{badge.value}</span>
                          <span className={`rounded-full border px-1.5 py-0.5 text-[10px] font-medium ${badge.className}`}>{badge.label}</span>
                        </div>
                      ) : (
                        <div className="mt-1 text-sm font-semibold text-white">n/a</div>
                      )}
                      <div className="mt-0.5 text-[10px] text-slate-500">{sub}</div>
                    </div>
                  ))}
                </div>
              </div>

              {aiResponse.references?.length ? (
                <div>
                  <div className="text-xs uppercase tracking-[0.2em] text-slate-400">References</div>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {aiResponse.references.map((ref) => (
                      <a
                        key={ref}
                        href={ref}
                        target="_blank"
                        rel="noopener noreferrer"
                        title={ref}
                        className="rounded-full border border-night-500 bg-night-800 px-2.5 py-1 text-xs text-brand-200 transition hover:border-brand-600/40 hover:text-brand-100"
                      >
                        {refLabel(ref)}
                      </a>
                    ))}
                  </div>
                </div>
              ) : null}

              {aiResponse.fix ? (
                <AiFixPanel fix={aiResponse.fix} />
              ) : null}
            </div>
          ) : (
            <div className="rounded-xl border border-night-500 bg-night-900 px-4 py-3 text-sm text-slate-400">
              No AI assessment is available for this finding.
            </div>
          )}
        </div>
      ) : null}

      {tab === "code" ? (
        <div className="mt-4">
          {isDast ? (
            <StepsToReproduce raw={finding.stepsToReproduce} />
          ) : (
            <CodeSnippet
              filePath={finding.filePath}
              sourceFileLink={finding.sourceFileLink}
              line={finding.line}
            />
          )}
        </div>
      ) : null}

      {tab === "notes" ? (
        <div className="mt-4 space-y-3">
          <PermissionGate action="comment">
            <div className="space-y-2">
              <textarea
                className="w-full rounded-xl border border-night-500 bg-night-900 px-3 py-2 text-xs text-slate-200 outline-none focus-visible:outline-none focus-visible:ring-0 focus-visible:border-brand-600"
                rows={3}
                placeholder="Add notes for this finding..."
                value={note}
                onChange={(event) => setNote(event.target.value)}
              />
              <div className="flex justify-end">
                <button
                  className="rounded-xl border border-night-500 bg-transparent px-3 py-2 text-xs text-white inline-flex items-center gap-2 disabled:opacity-50"
                  onClick={() => {
                    if (note.trim()) {
                      addNote.mutate(
                        { id: finding.id, entry: note },
                        {
                          onSuccess: () => {
                            setNote("");
                            toast.push("Note added.", "success");
                          },
                          onError: (error) => {
                            const message = error instanceof Error ? error.message : String(error);
                            toast.push(`Failed to add note: ${message}`, "error");
                          },
                        },
                      );
                    }
                  }}
                  disabled={!note.trim()}
                >
                  Add Notes
                </button>
              </div>
            </div>
          </PermissionGate>
          <div className="space-y-2 text-xs text-slate-300">
            {notesQuery.isLoading ? (
              <div>Loading notes...</div>
            ) : displayNotes.length > 0 ? (
              displayNotes.map((item) => (
                <div key={item.id} className="rounded-lg border border-night-500 bg-night-900 px-3 py-2">
                  <div className="text-slate-400">
                    {item.user_display ??
                      item.author_name ??
                      item.author?.username ??
                      ([item.author?.first_name, item.author?.last_name].filter(Boolean).join(" ") || "Unknown")} ·{" "}
                    {item.date ? new Date(item.date).toLocaleString() : ""}
                  </div>
                  <div className="mt-1 text-slate-200">{item.entry}</div>
                </div>
              ))
            ) : (
              <div>No notes yet.</div>
            )}
            {embedded && noteItems.length > displayNotes.length ? (
              <div className="text-xs text-slate-400">
                Showing first 5 notes. See full detail for more.
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      {tab === "history" ? (
        <div className="mt-4 space-y-3">
          {timelineQuery.isLoading ? (
            <div className="rounded-xl border border-night-500 bg-night-900 px-4 py-3 text-sm text-slate-400">
              Loading history...
            </div>
          ) : timelineQuery.isError ? (
            <div className="rounded-xl border border-danger-500/30 bg-night-900 px-4 py-3 text-sm text-danger-200">
              Failed to load history.
            </div>
          ) : timelineQuery.data && timelineQuery.data.length > 0 ? (
            <div className="space-y-0 overflow-hidden rounded-xl border border-night-500 bg-night-900">
              {(embedded ? timelineQuery.data.slice(0, EMBEDDED_HISTORY_ITEMS_LIMIT) : timelineQuery.data).map((event) => (
                <div
                  key={event.id}
                  className="grid grid-cols-[20px_minmax(0,1fr)] gap-3 border-b border-night-500/80 px-4 py-3 last:border-b-0"
                >
                  <div className="relative flex justify-center">
                    <span className="absolute inset-y-0 left-1/2 -translate-x-1/2 w-px bg-night-400/60 last:hidden" aria-hidden="true" />
                    <span className="relative mt-1.5 inline-block h-2.5 w-2.5 rounded-full bg-brand-500/90" />
                  </div>
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
                      <span className="rounded-full border border-night-500 bg-night-800 px-2 py-0.5 text-[11px] uppercase tracking-[0.12em] text-slate-300">
                        {HISTORY_BADGE_LABEL[event.eventType]}
                      </span>
                      <span>{event.owner || "System"}</span>
                      <span>•</span>
                      <span>{new Date(event.happenedAt).toLocaleString()}</span>
                    </div>
                    <div className="mt-2 text-sm text-slate-100 break-words">{event.details || "Updated"}</div>
                    {event.eventType === "finding_note_added" ? null : (
                      <div className="mt-2 text-xs text-slate-400">
                        Severity: <span className="text-slate-200">{event.severity || "Unknown"}</span>
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="rounded-xl border border-night-500 bg-night-900 px-4 py-3 text-sm text-slate-400">
              No history yet.
            </div>
          )}
          {embedded && timelineQuery.data && timelineQuery.data.length > EMBEDDED_HISTORY_ITEMS_LIMIT ? (
            <div className="text-xs text-slate-400">
              Showing latest {EMBEDDED_HISTORY_ITEMS_LIMIT} events. Open full detail for complete history.
            </div>
          ) : null}
        </div>
      ) : null}

      {tab === "work_items" ? (
        <div className="mt-4">
          <WorkItemsPanel findingId={finding.id} />
        </div>
      ) : null}
    </div>
  );
}
