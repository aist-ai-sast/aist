import { Link, useParams } from "react-router-dom";
import { useState } from "react";

import CodeSnippet from "../components/CodeSnippet";
import {
  useAiResponse,
  useFinding,
  useFindingNotes,
  usePipelines,
  useProjectMeta,
  useProjects,
} from "../lib/queries";
import { useAddFindingNote, useExportAiResults, useUpdateFindingStatus } from "../lib/mutations";
import { useToast } from "../components/ToastProvider";

export default function FindingDetailPage() {
  const params = useParams();
  const findingId = params.id ? Number(params.id) : undefined;
  const findingQuery = useFinding(findingId);
  const projectsQuery = useProjects();
  const finding = findingQuery.data ?? undefined;
  const [note, setNote] = useState("");
  const updateStatus = useUpdateFindingStatus();
  const addNote = useAddFindingNote();
  const exportAi = useExportAiResults();
  const notesQuery = useFindingNotes(findingId);
  const toast = useToast();

  const projects = projectsQuery.data ?? [];
  const aistProject = projects.find((project) => project.productId === finding?.productId);
  const productName = projects.find((project) => project.productId === finding?.productId)?.name;
  const pipelinesQuery = usePipelines(aistProject?.id);
  const aiResponse = useAiResponse(pipelinesQuery.data ?? [], finding?.id);
  const metaQuery = useProjectMeta(aistProject?.id);
  const projectVersionId = metaQuery.data?.versions?.length
    ? Number(metaQuery.data.versions[metaQuery.data.versions.length - 1].id)
    : undefined;

  if (findingQuery.isLoading) {
    return (
      <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
        Loading finding...
      </div>
    );
  }

  if (!finding) {
    return (
      <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
        Finding not found. <Link to="/">Back to Findings</Link>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="text-xs uppercase tracking-[0.2em] text-slate-400">
            Finding Detail
          </div>
          <h1 className="mt-2 text-2xl font-semibold">{finding.title}</h1>
          <div className="mt-2 flex flex-wrap gap-3 text-xs text-slate-400">
            <span>Severity: {finding.severity}</span>
            <span>Status: {finding.active ? "Enabled" : "Disabled"}</span>
            <span>Product: {productName ?? finding.product}</span>
          </div>
        </div>
        <div className="flex gap-2">
          <button
            className="rounded-xl border border-night-500 bg-night-700 px-4 py-2 text-xs text-white"
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
          <button
            className="rounded-xl border border-night-500 bg-night-700 px-4 py-2 text-xs text-white"
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
            Comment
          </button>
          <button
            className="rounded-xl bg-brand-500 px-4 py-2 text-xs font-semibold text-night-900 disabled:opacity-50"
            onClick={() => {
              if (!pipelinesQuery.data?.[0]?.id) {
                toast.push("No pipeline available for export.", "error");
                return;
              }
              exportAi.mutate(
                { pipelineId: pipelinesQuery.data[0].id },
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
            disabled={!pipelinesQuery.data?.[0]?.id}
          >
            Export
          </button>
        </div>
      </div>

      <section className="rounded-2xl border border-night-500 bg-night-700 p-5 shadow-panel">
        <div className="text-xs uppercase tracking-[0.2em] text-slate-400">
          AI Summary
        </div>
        <p className="mt-3 text-sm text-slate-200">
          {aiResponse?.reasoning ?? "AI reasoning and evidence from the pipeline response will be displayed here."}
        </p>
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
      </section>

      <section className="rounded-2xl border border-night-500 bg-night-700 p-5 shadow-panel">
        <div className="text-xs uppercase tracking-[0.2em] text-slate-400">
          Code Viewer
        </div>
        <div className="mt-3">
          <CodeSnippet
            projectVersionId={projectVersionId}
            filePath={finding.filePath}
            line={finding.line}
          />
        </div>
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-2xl border border-night-500 bg-night-700 p-5 shadow-panel">
          <div className="text-xs uppercase tracking-[0.2em] text-slate-400">
            Metadata
          </div>
          <div className="mt-3 space-y-2 text-xs text-slate-300">
            <div>Tool: {finding.tool}</div>
            <div>File: {finding.filePath}</div>
            <div>Line: {finding.line}</div>
          </div>
        </div>
        <div className="rounded-2xl border border-night-500 bg-night-700 p-5 shadow-panel">
          <div className="text-xs uppercase tracking-[0.2em] text-slate-400">
            Notes
          </div>
          <textarea
            className="mt-3 w-full rounded-xl border border-night-500 bg-night-900 px-3 py-2 text-xs text-slate-200"
            rows={4}
            placeholder="Add a comment for this finding..."
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />
          <div className="mt-4 space-y-3 text-xs text-slate-300">
            {notesQuery.isLoading ? (
              <div>Loading notes...</div>
            ) : notesQuery.data && notesQuery.data.length > 0 ? (
              notesQuery.data.map((item) => (
                <div key={item.id} className="rounded-lg border border-night-500 bg-night-900 px-3 py-2">
                  <div className="text-slate-400">
                    {item.author?.username ?? "Unknown"} ·{" "}
                    {item.date ? new Date(item.date).toLocaleString() : ""}
                  </div>
                  <div className="mt-1 text-slate-200">{item.entry}</div>
                </div>
              ))
            ) : (
              <div>No comments yet.</div>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
