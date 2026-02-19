import { useState } from "react";

import type { AIResponse, Finding } from "../types";
import CodeSnippet from "./CodeSnippet";
import DescriptionBlock from "./DescriptionBlock";
import { useAddFindingNote } from "../lib/mutations";
import { useFinding, useFindingNotes } from "../lib/queries";
import PermissionGate from "./PermissionGate";
import { useToast } from "./ToastProvider";
import { ACCENT_SELECTED_CLASS } from "../lib/uiClasses";

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

type TabId = "overview" | "ai" | "code" | "notes";

const tabs: Array<{ id: TabId; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "ai", label: "AI Assessment" },
  { id: "code", label: "Code" },
  { id: "notes", label: "Notes" },
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

function formatScore(value?: number) {
  if (typeof value !== "number" || Number.isNaN(value)) return "n/a";
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

function formatConfidence(uncertaintyLevel?: number) {
  if (typeof uncertaintyLevel !== "number" || Number.isNaN(uncertaintyLevel)) return "n/a";
  const bounded = Math.max(0, Math.min(1, uncertaintyLevel));
  return `${Math.round((1 - bounded) * 100)}%`;
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
  const resolvedTags =
    finding.tags && finding.tags.length > 0
      ? finding.tags
      : detailQuery.data?.tags ?? [];
  const tagsLoading = !finding.tags?.length && detailQuery.isLoading;

  return (
    <div>
      <div className="flex flex-wrap gap-2">
        {tabs.map((item) => (
          <button
            key={item.id}
            type="button"
            className={[
              "rounded-full border px-3 py-1 text-xs",
              tab === item.id
                ? ACCENT_SELECTED_CLASS
                : "border-night-500 bg-night-900 text-slate-300",
            ].join(" ")}
            onClick={() => setTab(item.id)}
          >
            {item.label}
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

          {finding.cwe ? (
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
                  {finding.cwe}
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
                <span className="text-xs uppercase tracking-[0.2em] text-slate-400">AI Assessment</span>
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
                <span className="rounded-full border border-night-500 bg-night-800 px-3 py-1 text-xs text-slate-300">
                  Confidence: {formatConfidence(aiResponse.uncertaintyLevel)}
                </span>
              </div>

              {aiResponse.title ? (
                <div className="text-base font-semibold text-white">{aiResponse.title}</div>
              ) : null}

              <div>
                <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Executive Summary</div>
                <div className="mt-2 rounded-xl border border-night-500 bg-night-800/70 px-4 py-3 leading-relaxed text-slate-200">
                  {aiResponse.reasoning}
                </div>
              </div>

              <div>
                <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Risk Signals</div>
                <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                  <div className="rounded-xl border border-night-500 bg-night-800 px-3 py-2 text-xs">
                    <div className="text-slate-400">EPSS</div>
                    <div className="mt-1 text-sm font-semibold text-white">{formatScore(aiResponse.epssScore)}</div>
                  </div>
                  <div className="rounded-xl border border-night-500 bg-night-800 px-3 py-2 text-xs">
                    <div className="text-slate-400">Impact</div>
                    <div className="mt-1 text-sm font-semibold text-white">{formatScore(aiResponse.impactScore)}</div>
                  </div>
                  <div className="rounded-xl border border-night-500 bg-night-800 px-3 py-2 text-xs">
                    <div className="text-slate-400">Exploitability</div>
                    <div className="mt-1 text-sm font-semibold text-white">{formatScore(aiResponse.exploitabilityScore)}</div>
                  </div>
                  <div className="rounded-xl border border-night-500 bg-night-800 px-3 py-2 text-xs">
                    <div className="text-slate-400">Uncertainty</div>
                    <div className="mt-1 text-sm font-semibold text-white">{formatScore(aiResponse.uncertaintyLevel)}</div>
                  </div>
                </div>
              </div>

              {aiResponse.references?.length ? (
                <div>
                  <div className="text-xs uppercase tracking-[0.2em] text-slate-400">References</div>
                  <ul className="mt-2 list-disc space-y-1 pl-5 text-xs">
                    {aiResponse.references.map((ref) => (
                      <li key={ref} className="text-slate-300">
                        <a
                          href={ref}
                          target="_blank"
                          rel="noreferrer"
                          className="text-brand-200 underline-offset-2 transition hover:text-brand-100 hover:underline"
                          title={ref}
                        >
                          {ref}
                        </a>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}

              <div className="rounded-xl border border-night-500 bg-night-800 px-3 py-2 text-xs text-slate-400">
                AI-assisted recommendation. Final triage decision remains with the security analyst.
              </div>
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
          <CodeSnippet
            filePath={finding.filePath}
            sourceFileLink={finding.sourceFileLink}
            line={finding.line}
          />
        </div>
      ) : null}

      {tab === "notes" ? (
        <div className="mt-4 space-y-3">
          <PermissionGate action="comment" productId={permissionProductId}>
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
                            toast.push("Notes added.", "success");
                          },
                          onError: (error) => {
                            const message = error instanceof Error ? error.message : String(error);
                            toast.push(`Notes failed: ${message}`, "error");
                          },
                        },
                      );
                      setNote("");
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
                    {item.author?.username ?? "Unknown"} ·{" "}
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
    </div>
  );
}
