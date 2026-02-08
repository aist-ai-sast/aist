import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { usePipelineSummaries, useProjects } from "../lib/queries";
import type { PipelineSummary } from "../types";
import PipelineFilterPanel from "../components/PipelineFilterPanel";

const statusOptions = [
  { value: "all", label: "All statuses" },
  { value: "SAST_LAUNCHED", label: "Launched" },
  { value: "UPLOADING_RESULTS", label: "Uploading Results" },
  { value: "FINDING_POSTPROCESSING", label: "Finding Post-processing" },
  { value: "WAITING_DEDUPLICATION_TO_FINISH", label: "Waiting Deduplication" },
  { value: "WAITING_CONFIRMATION_TO_PUSH_TO_AI", label: "Waiting AI Confirmation" },
  { value: "PUSH_TO_AI", label: "Push to AI" },
  { value: "WAITING_RESULT_FROM_AI", label: "Waiting AI Result" },
  { value: "FINISHED", label: "Finished" },
];

function formatDate(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString();
}

function truncateText(value: string | null | undefined, max = 32) {
  if (!value) return "—";
  if (value.length <= max) return value;
  return `${value.slice(0, max - 3)}...`;
}

function statusBadge(status: string) {
  const upper = status.toUpperCase();
  if (upper.includes("FAIL")) return "border-danger-500/50 text-danger-500 bg-danger-500/10";
  if (upper.includes("FINISH")) return "border-brand-600/50 text-brand-500 bg-brand-600/10";
  if (upper.includes("START")) return "border-brand-600/50 text-brand-500 bg-brand-600/10";
  return "border-slate-500/40 text-slate-300 bg-night-700";
}

function ActionsBadge({ actions }: { actions: PipelineSummary["actions"] }) {
  if (!actions.length) {
    return <span className="text-xs text-slate-400">No actions</span>;
  }
  return (
    <div className="flex flex-wrap gap-2">
      {actions.slice(0, 3).map((action, idx) => (
        <span
          key={`${action.type ?? "action"}-${idx}`}
          className="rounded-full border border-night-500 bg-night-900 px-3 py-1 text-xs text-slate-200"
        >
          {action.type ?? "Action"} · {action.status ?? "pending"}
        </span>
      ))}
      {actions.length > 3 ? (
        <span className="text-xs text-slate-400">+{actions.length - 3} more</span>
      ) : null}
    </div>
  );
}

export default function PipelinesPage() {
  const projectsQuery = useProjects();
  const [searchParams, setSearchParams] = useSearchParams();
  const [selectedProductId, setSelectedProductId] = useState<number | undefined>();
  const [status, setStatus] = useState("all");
  const [search, setSearch] = useState("");
  const [createdFrom, setCreatedFrom] = useState("");
  const [createdTo, setCreatedTo] = useState("");
  const [selectedPipeline, setSelectedPipeline] = useState<PipelineSummary | null>(null);

  const projects = projectsQuery.data ?? [];
  useEffect(() => {
    if (!selectedProductId) return;
    const exists = projects.some((project) => project.productId === selectedProductId);
    if (!exists) {
      setSelectedProductId(undefined);
    }
  }, [projects, selectedProductId]);

  useEffect(() => {
    const productParam = searchParams.get("product");
    if (!productParam) return;
    const parsed = Number(productParam);
    if (!Number.isNaN(parsed)) {
      setSelectedProductId(parsed);
    }
    setSearchParams(
      (params) => {
        params.delete("product");
        return params;
      },
      { replace: true },
    );
  }, [searchParams, setSearchParams]);

  const pipelinesQuery = usePipelineSummaries({
    productId: selectedProductId,
    status: status !== "all" ? status : undefined,
    createdGte: createdFrom || undefined,
    createdLte: createdTo || undefined,
    search: search || undefined,
    ordering: "-created",
  });

  const pipelines = pipelinesQuery.data?.items ?? [];
  useEffect(() => {
    if (pipelines.length && !selectedPipeline) {
      setSelectedPipeline(pipelines[0]);
    }
  }, [pipelines, selectedPipeline]);

  const productOptions = useMemo(
    () =>
      projects.map((project) => ({
        value: String(project.productId),
        label: project.name,
      })),
    [projects],
  );

  const summary = useMemo(() => {
    const byStatus: Record<string, number> = {};
    pipelines.forEach((item) => {
      byStatus[item.status] = (byStatus[item.status] ?? 0) + 1;
    });
    return {
      total: pipelines.length,
      finished: byStatus.FINISHED ?? 0,
      inProgress:
        (byStatus.SAST_LAUNCHED ?? 0) +
        (byStatus.UPLOADING_RESULTS ?? 0) +
        (byStatus.FINDING_POSTPROCESSING ?? 0) +
        (byStatus.WAITING_DEDUPLICATION_TO_FINISH ?? 0) +
        (byStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI ?? 0) +
        (byStatus.PUSH_TO_AI ?? 0) +
        (byStatus.WAITING_RESULT_FROM_AI ?? 0),
    };
  }, [pipelines]);

  return (
    <div className="grid min-h-0 gap-6 lg:grid-cols-[280px_1fr_360px]">
      <div className="lg:sticky lg:top-24 self-start max-h-[calc(100vh-140px)] overflow-auto">
        <PipelineFilterPanel
          productOptions={productOptions}
          selectedProductId={selectedProductId}
          onProductChange={setSelectedProductId}
          status={status}
          onStatusChange={setStatus}
          search={search}
          onSearchChange={setSearch}
          createdFrom={createdFrom}
          onCreatedFromChange={setCreatedFrom}
          createdTo={createdTo}
          onCreatedToChange={setCreatedTo}
          statusOptions={statusOptions}
        />
      </div>

      <div className="space-y-5">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Pipelines</div>
            <div className="mt-2 text-2xl font-semibold text-white">
              {pipelines.length} pipelines
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <span className="rounded-full border border-night-500 bg-night-800 px-3 py-1 text-xs text-slate-200">
                Total: {summary.total}
              </span>
              <span className="rounded-full border border-brand-600/50 bg-brand-600/10 px-3 py-1 text-xs text-brand-500">
                Finished: {summary.finished}
              </span>
              <span className="rounded-full border border-night-500 bg-night-800 px-3 py-1 text-xs text-slate-200">
                In progress: {summary.inProgress}
              </span>
            </div>
          </div>
        </div>

        {pipelinesQuery.isLoading ? (
          <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
            Loading pipelines...
          </div>
        ) : pipelines.length === 0 ? (
          <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
            No pipelines match the current filters.
          </div>
        ) : (
          <div className="space-y-4">
            {pipelines.map((pipeline) => (
              <article
                key={pipeline.id}
                className={[
                  "rounded-2xl border bg-night-700/80 p-5 shadow-panel transition",
                  selectedPipeline?.id === pipeline.id
                    ? "border-brand-600/70 shadow-[0_0_0_1px_rgba(77,212,255,0.25)]"
                    : "border-night-500 hover:border-brand-600/50",
                ].join(" ")}
                onClick={() => setSelectedPipeline(pipeline)}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-3">
                    <span
                      className={[
                        "rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-wide",
                        statusBadge(pipeline.status),
                      ].join(" ")}
                    >
                      {pipeline.status}
                    </span>
                    <span className="text-xs text-slate-400">Pipeline {pipeline.id}</span>
                  </div>
                  <span className="text-xs text-slate-400">{pipeline.productName}</span>
                </div>
                <div className="mt-3 grid gap-2 text-xs text-slate-400 md:grid-cols-3">
                  <div>
                    <div className="text-[10px] uppercase tracking-[0.18em] text-slate-500">Started</div>
                    <div className="text-slate-200">{formatDate(pipeline.started)}</div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-[0.18em] text-slate-500">Finished</div>
                    <div className="text-slate-200">{formatDate(pipeline.updated)}</div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-[0.18em] text-slate-500">Findings</div>
                    <div className="text-slate-200">{pipeline.findings}</div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-[0.18em] text-slate-500">Branch</div>
                    <div className="text-slate-200" title={pipeline.branch ?? undefined}>
                      {truncateText(pipeline.branch, 28)}
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-[0.18em] text-slate-500">Commit</div>
                    <div className="text-slate-200" title={pipeline.commit ?? undefined}>
                      {truncateText(pipeline.commit, 16)}
                    </div>
                  </div>
                </div>
                <div className="mt-3">
                  <ActionsBadge actions={pipeline.actions} />
                </div>
                <div className="mt-4 flex gap-2">
                  <Link
                    to={`/?pipeline=${pipeline.id}`}
                    className="rounded-xl border border-night-500 px-3 py-2 text-xs text-slate-200"
                  >
                    Open Findings
                  </Link>
                </div>
              </article>
            ))}
          </div>
        )}
      </div>

      <aside className="rounded-2xl border border-night-500 bg-night-700 p-5 shadow-panel lg:sticky lg:top-24 self-start max-h-[calc(100vh-140px)] overflow-auto">
        {!selectedPipeline ? (
          <div className="text-sm text-slate-400">Select a pipeline to view details.</div>
        ) : (
          <div className="space-y-4 text-xs text-slate-300">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Pipeline Detail</div>
                <div className="mt-2 text-base font-semibold text-white" title={selectedPipeline.id}>
                  {truncateText(selectedPipeline.id, 20)}
                </div>
                <div className="mt-1 text-xs text-slate-400">{selectedPipeline.productName}</div>
              </div>
              <span
                className={[
                  "rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-wide",
                  statusBadge(selectedPipeline.status),
                ].join(" ")}
              >
                {selectedPipeline.status}
              </span>
            </div>

            <div className="rounded-xl border border-night-500 bg-night-900 px-4 py-3">
              <div className="grid gap-3">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] uppercase tracking-[0.18em] text-slate-500">Started</span>
                  <span className="text-slate-200">{formatDate(selectedPipeline.started)}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[10px] uppercase tracking-[0.18em] text-slate-500">Finished</span>
                  <span className="text-slate-200">{formatDate(selectedPipeline.updated)}</span>
                </div>
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] uppercase tracking-[0.18em] text-slate-500">Branch</span>
                    <span className="text-slate-200" title={selectedPipeline.branch ?? undefined}>
                      {truncateText(selectedPipeline.branch, 24)}
                    </span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] uppercase tracking-[0.18em] text-slate-500">Commit</span>
                    <span className="text-slate-200" title={selectedPipeline.commit ?? undefined}>
                      {truncateText(selectedPipeline.commit, 24)}
                    </span>
                  </div>
                <div className="flex items-center justify-between">
                  <span className="text-[10px] uppercase tracking-[0.18em] text-slate-500">Findings</span>
                  <span className="text-slate-200">{selectedPipeline.findings}</span>
                </div>
              </div>
            </div>

            <div>
              <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Actions</div>
              <div className="mt-2">
                <ActionsBadge actions={selectedPipeline.actions} />
              </div>
            </div>

            <Link
              to={`/?pipeline=${selectedPipeline.id}`}
              className="inline-flex rounded-xl border border-night-500 px-3 py-2 text-xs text-slate-200"
            >
              Open Findings
            </Link>
          </div>
        )}
      </aside>
    </div>
  );
}
