import { useState } from "react";

import type { AIResponse, Finding } from "../types";
import CodeSnippet from "./CodeSnippet";
import DescriptionBlock from "./DescriptionBlock";
import { useAddFindingNote, useExportAiResults, useUpdateFindingStatus } from "../lib/mutations";
import { useFindingNotes } from "../lib/queries";
import { useToast } from "./ToastProvider";
import PermissionGate from "./PermissionGate";

type DetailPanelProps = {
  finding?: Finding;
  aiResponse?: AIResponse | null;
  pipelineId?: string;
};

const riskLabels = {
  risk_accepted: "Risk Accepted",
  under_review: "Under Review",
  mitigated: "Mitigated",
};

export default function DetailPanel({ finding, aiResponse, pipelineId }: DetailPanelProps) {
  const [note, setNote] = useState("");
  const addNote = useAddFindingNote();
  const updateStatus = useUpdateFindingStatus();
  const exportAi = useExportAiResults();
  const notesQuery = useFindingNotes(finding?.id);
  const toast = useToast();
  if (!finding) {
    return (
      <aside className="rounded-2xl border border-night-500 bg-night-700 p-5 text-sm text-slate-400">
        Select a finding to view detail.
      </aside>
    );
  }

  return (
    <aside className="rounded-2xl border border-night-500 bg-night-700 p-5 shadow-panel">
      <div className="text-xs uppercase tracking-[0.2em] text-slate-400">
        Selected Finding
      </div>
      <h2
        className="mt-3 text-lg font-semibold text-white line-clamp-2"
        title={finding.title}
      >
        {finding.title}
      </h2>
      <div className="mt-2 flex flex-wrap gap-2 text-xs text-slate-400">
        <span>Severity: {finding.severity}</span>
        <span>Status: {finding.active ? "Active" : "Non-Active"}</span>
        {finding.cwe ? <span>CWE: {finding.cwe}</span> : null}
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        {finding.riskStates?.map((risk) => (
          <span
            key={risk}
            className="rounded-full border border-amber-400/40 bg-amber-400/10 px-3 py-1 text-xs text-amber-400"
          >
            {riskLabels[risk]}
          </span>
        ))}
      </div>
      <div className="mt-4 rounded-xl border border-brand-600/40 bg-brand-600/10 px-4 py-3 text-sm text-slate-200">
        {aiResponse?.reasoning ?? "AI comment will appear here when a response is attached to this finding."}
        {aiResponse ? (
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
        ) : null}
      </div>
      {finding.tags?.length ? (
        <div className="mt-4">
          <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Tags</div>
          <div className="mt-2 flex flex-wrap gap-2">
            {finding.tags.map((tag) => (
              <span
                key={tag}
                className="rounded-full border border-night-500 bg-night-900 px-3 py-1 text-xs text-slate-200"
              >
                {tag}
              </span>
            ))}
          </div>
        </div>
      ) : null}
      <div className="mt-4">
        <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Description</div>
        <div className="mt-2 rounded-xl border border-night-500 bg-night-900 px-4 py-3">
          <DescriptionBlock value={finding.description} />
        </div>
      </div>
      <div className="mt-4">
        <CodeSnippet
          projectVersionId={finding.projectVersionId}
          filePath={finding.filePath}
          sourceFileLink={finding.sourceFileLink}
          line={finding.line}
        />
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        <PermissionGate action="enable" productId={finding?.productId}>
          <button
            className="rounded-xl border border-night-500 bg-transparent px-3 py-2 text-xs text-white"
            onClick={() =>
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
              )
            }
          >
            {finding.active ? "Disable" : "Enable"}
          </button>
        </PermissionGate>
        <PermissionGate action="comment" productId={finding?.productId}>
          <button
            className="rounded-xl border border-night-500 bg-transparent px-3 py-2 text-xs text-white"
            onClick={() => {
              if (note.trim()) {
                addNote.mutate(
                  { id: finding.id, entry: note },
                  {
                    onSuccess: () => {
                      toast.push("Comment added.", "success");
                    },
                    onError: (error) => {
                      const message = error instanceof Error ? error.message : String(error);
                      toast.push(`Comment failed: ${message}`, "error");
                    },
                  },
                );
                setNote("");
              }
            }}
          >
            Add Comment
          </button>
        </PermissionGate>
        <button
          className="rounded-xl bg-brand-500 px-3 py-2 text-xs font-semibold text-night-900 disabled:opacity-50"
          onClick={() => {
            if (!pipelineId) {
              toast.push("No pipeline available for export.", "error");
              return;
            }
            exportAi.mutate(
              { pipelineId },
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
          disabled={!pipelineId}
        >
          Export
        </button>
      </div>
      <PermissionGate action="comment" productId={finding?.productId}>
        <textarea
          className="mt-3 w-full rounded-xl border border-night-500 bg-night-900 px-3 py-2 text-xs text-slate-200"
          rows={3}
          placeholder="Add a comment for this finding..."
          value={note}
          onChange={(event) => setNote(event.target.value)}
        />
      </PermissionGate>
      <div className="mt-4 text-xs text-slate-400 uppercase tracking-[0.2em]">
        Notes
      </div>
      <div className="mt-2 space-y-2 text-xs text-slate-300">
        {notesQuery.isLoading ? (
          <div>Loading notes...</div>
        ) : notesQuery.data && notesQuery.data.length > 0 ? (
          notesQuery.data.slice(0, 3).map((item) => (
            <div key={item.id} className="rounded-lg border border-night-500 bg-night-900 px-3 py-2">
              <div className="text-slate-400">
                {item.author?.username ?? "Unknown"} · {item.date ? new Date(item.date).toLocaleString() : ""}
              </div>
              <div className="mt-1 text-slate-200">{item.entry}</div>
            </div>
          ))
        ) : (
          <div>No comments yet.</div>
        )}
      </div>
      <a href={`/finding/${finding.id}`} className="mt-4 inline-flex text-sm text-brand-500">
        Open full detail →
      </a>
    </aside>
  );
}
