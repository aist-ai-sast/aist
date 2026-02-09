import { useState } from "react";

import type { AIResponse, Finding } from "../types";
import CodeSnippet from "./CodeSnippet";
import DescriptionBlock from "./DescriptionBlock";
import { useAddFindingNote } from "../lib/mutations";
import { useFinding, useFindingNotes } from "../lib/queries";
import PermissionGate from "./PermissionGate";
import { useToast } from "./ToastProvider";

type FindingDetailTabsProps = {
  finding: Finding;
  aiResponse?: AIResponse | null;
  embedded?: boolean;
  selectedTags?: string[];
  onToggleTag?: (tag: string) => void;
  selectedCwe?: string;
  onToggleCwe?: (cwe: string) => void;
};

type TabId = "overview" | "code" | "notes";

const tabs: Array<{ id: TabId; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "code", label: "Code" },
  { id: "notes", label: "Notes" },
];

export default function FindingDetailTabs({
  finding,
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
                ? "border-brand-600/70 bg-brand-600/20 text-brand-400"
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
          {aiResponse ? (
            <div className="rounded-xl border border-brand-600/40 bg-brand-600/10 px-4 py-3 text-sm text-slate-200">
              {aiResponse.reasoning}
              <div className="mt-3 grid gap-2 text-xs text-slate-300">
                <div>EPSS: {aiResponse.epssScore ?? "n/a"}</div>
                <div>Impact: {aiResponse.impactScore ?? "n/a"}</div>
                <div>Exploitability: {aiResponse.exploitabilityScore ?? "n/a"}</div>
                {aiResponse.references?.length ? (
                  <div>
                    References:
                    <ul className="mt-1 list-disc pl-4">
                      {aiResponse.references.map((ref) => (
                        <li key={ref}>{ref}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}

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
                        ? "border-brand-600/70 bg-brand-600/20 text-brand-400"
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
                      ? "border-brand-600/70 bg-brand-600/20 text-brand-400"
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

      {tab === "code" ? (
        <div className="mt-4">
          <CodeSnippet
            projectVersionId={finding.projectVersionId}
            filePath={finding.filePath}
            sourceFileLink={finding.sourceFileLink}
            line={finding.line}
          />
        </div>
      ) : null}

      {tab === "notes" ? (
        <div className="mt-4 space-y-3">
          <PermissionGate action="comment" productId={finding?.productId}>
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
