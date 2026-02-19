import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { usePipelineSummaries, useProjects } from "../lib/queries";
import type { PipelineSummary } from "../types";
import PipelineFilterPanel from "../components/PipelineFilterPanel";
import PaginationBar from "../components/PaginationBar";
import { getRoute } from "../lib/routes";

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

function findingsPath(params: {
  project?: number;
  pipeline?: string;
  project_version?: string;
}) {
  const search = new URLSearchParams();
  if (params.project) search.set("project", String(params.project));
  if (params.pipeline) search.set("pipeline", params.pipeline);
  if (params.project_version) search.set("project_version", params.project_version);
  const query = search.toString();
  return query ? `${getRoute("ui_findings_path")}?${query}` : getRoute("ui_findings_path");
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

function PipelineDetailCard({ pipeline }: { pipeline: PipelineSummary | null }) {
  if (!pipeline) {
    return <div className="text-sm text-slate-400">Select a pipeline to view details.</div>;
  }
  return (
    <div className="space-y-4 text-xs text-slate-300">
      <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Pipeline Detail</div>
      <div className="rounded-xl border border-night-500 bg-night-900 px-4 py-3">
        <div className="grid gap-3">
          <div className="flex items-center justify-between">
            <span className="text-[10px] uppercase tracking-[0.18em] text-slate-500">Branch</span>
            {pipeline.branch ? (
              <Link
                to={findingsPath({ project: pipeline.projectId, project_version: pipeline.branch })}
                className="aist-clickable-text"
                title={pipeline.branch}
              >
                {truncateText(pipeline.branch, 24)}
              </Link>
            ) : (
              <span className="text-slate-200">—</span>
            )}
          </div>
          <div className="flex items-center justify-between">
            <span className="text-[10px] uppercase tracking-[0.18em] text-slate-500">Commit</span>
            {pipeline.commit ? (
              <Link
                to={findingsPath({ project: pipeline.projectId, project_version: pipeline.commit })}
                className="aist-clickable-text font-mono"
                title={pipeline.commit}
              >
                {truncateText(pipeline.commit, 24)}
              </Link>
            ) : (
              <span className="text-slate-200">—</span>
            )}
          </div>
        </div>
      </div>

      <div>
        <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Actions</div>
        <div className="mt-2">
          <ActionsBadge actions={pipeline.actions} />
        </div>
      </div>

      <Link
        to={findingsPath({ pipeline: pipeline.id, project: pipeline.projectId })}
        className="inline-flex rounded-xl border border-night-500 px-3 py-2 text-xs text-slate-200"
      >
        Open Findings
      </Link>
    </div>
  );
}

export default function PipelinesPage() {
  const navigate = useNavigate();
  const projectsQuery = useProjects();
  const [searchParams] = useSearchParams();
  const [selectedProjectId, setSelectedProjectId] = useState<number | undefined>();
  const [status, setStatus] = useState("all");
  const [search, setSearch] = useState("");
  const [createdFrom, setCreatedFrom] = useState("");
  const [createdTo, setCreatedTo] = useState("");
  const [pageSize, setPageSize] = useState<number>(25);
  const [pageIndex, setPageIndex] = useState<number>(0);
  const [expandedPipelineId, setExpandedPipelineId] = useState<string | null>(null);

  const projects = projectsQuery.data ?? [];
  useEffect(() => {
    if (!selectedProjectId) return;
    const exists = projects.some((project) => project.id === selectedProjectId);
    if (!exists) {
      setSelectedProjectId(undefined);
    }
  }, [projects, selectedProjectId]);

  useEffect(() => {
    const projectParam = searchParams.get("project");
    if (!projectParam) return;
    const parsed = Number(projectParam);
    if (!Number.isNaN(parsed)) {
      setSelectedProjectId(parsed);
    }
  }, [searchParams]);

  const pipelinesQuery = usePipelineSummaries({
    projectId: selectedProjectId,
    status: status !== "all" ? status : undefined,
    createdGte: createdFrom || undefined,
    createdLte: createdTo || undefined,
    search: search || undefined,
    ordering: "-created",
    limit: pageSize,
    offset: pageIndex * pageSize,
  });

  const pipelines = pipelinesQuery.data?.items ?? [];
  useEffect(() => {
    if (!expandedPipelineId || pipelines.find((item) => item.id === expandedPipelineId)) return;
    setExpandedPipelineId(null);
  }, [pipelines, expandedPipelineId]);

  const projectOptions = useMemo(
    () =>
      projects.map((project) => ({
        value: String(project.id),
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

  useEffect(() => {
    setPageIndex(0);
  }, [selectedProjectId, status, search, createdFrom, createdTo, pageSize]);

  return (
    <div className="grid min-h-0 gap-6 lg:grid-cols-[280px_1fr]">
      <div className="aist-scrollbar lg:sticky lg:top-24 self-start lg:max-h-[calc(100vh-140px)] lg:overflow-auto">
        <PipelineFilterPanel
          projectOptions={projectOptions}
          selectedProjectId={selectedProjectId}
          onProjectChange={setSelectedProjectId}
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

        <div className="flex min-h-[calc(100vh-280px)] flex-col">
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
                    "p-5 aist-card aist-card--interactive",
                    expandedPipelineId === pipeline.id ? "aist-card--expanded" : "",
                  ].join(" ")}
                  role="button"
                  tabIndex={0}
                  aria-expanded={expandedPipelineId === pipeline.id}
                  onClick={() =>
                    setExpandedPipelineId((current) =>
                      current === pipeline.id ? null : pipeline.id,
                    )
                  }
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      setExpandedPipelineId((current) =>
                        current === pipeline.id ? null : pipeline.id,
                      );
                    }
                  }}
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
                    <div className="flex items-center gap-2 text-xs text-slate-400">
                      <button
                        type="button"
                        className="aist-clickable-text"
                        onClick={(event) => {
                          event.stopPropagation();
                          navigate(findingsPath({ project: pipeline.projectId }));
                        }}
                      >
                        {pipeline.productName}
                      </button>
                      <span
                        className={[
                          "inline-flex h-6 w-6 items-center justify-center rounded-full border border-night-500 bg-night-800 text-slate-300 transition-transform",
                          expandedPipelineId === pipeline.id ? "rotate-180" : "",
                        ].join(" ")}
                        aria-hidden="true"
                      >
                        <svg viewBox="0 0 24 24" className="h-4 w-4">
                          <path fill="currentColor" d="M7 10l5 5 5-5H7Z" />
                        </svg>
                      </span>
                    </div>
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
                      {pipeline.branch ? (
                        <Link
                          to={findingsPath({ project: pipeline.projectId, project_version: pipeline.branch })}
                          className="aist-clickable-text"
                          title={pipeline.branch}
                          onClick={(event) => event.stopPropagation()}
                        >
                          {truncateText(pipeline.branch, 28)}
                        </Link>
                      ) : (
                        <div className="text-slate-200">—</div>
                      )}
                    </div>
                    <div>
                      <div className="text-[10px] uppercase tracking-[0.18em] text-slate-500">Commit</div>
                      {pipeline.commit ? (
                        <Link
                          to={findingsPath({ project: pipeline.projectId, project_version: pipeline.commit })}
                          className="aist-clickable-text font-mono"
                          title={pipeline.commit}
                          onClick={(event) => event.stopPropagation()}
                        >
                          {truncateText(pipeline.commit, 16)}
                        </Link>
                      ) : (
                        <div className="text-slate-200">—</div>
                      )}
                    </div>
                  </div>
                  <div className="mt-3">
                    <ActionsBadge actions={pipeline.actions} />
                  </div>
                  <div className="mt-4 flex gap-2">
                    <Link
                      to={findingsPath({ pipeline: pipeline.id, project: pipeline.projectId })}
                      className="rounded-xl border border-night-500 px-3 py-2 text-xs text-slate-200"
                    >
                      Open Findings
                    </Link>
                  </div>
                  <div className="mt-4">
                    {expandedPipelineId === pipeline.id ? (
                      <div
                        className="panel-collapse"
                        data-state="open"
                        onClick={(event) => event.stopPropagation()}
                        onKeyDown={(event) => event.stopPropagation()}
                      >
                        <div className="panel-collapse-inner">
                          <PipelineDetailCard pipeline={pipeline} />
                        </div>
                      </div>
                    ) : null}
                  </div>
                </article>
              ))}
            </div>
          )}
          {pipelinesQuery.data ? (
            <div className="mt-auto">
              <PaginationBar
                count={pipelinesQuery.data.count}
                noun="pipelines"
                pageIndex={pageIndex}
                pageSize={pageSize}
                onPageIndexChange={setPageIndex}
                onPageSizeChange={setPageSize}
                rowOptions={[10, 25, 50]}
              />
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
