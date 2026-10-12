import { formatDistanceToNowStrict } from "date-fns";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { usePipelineSummaries, useProjects } from "../lib/queries";
import type { PipelineSummary } from "../types";
import PipelineFilterPanel from "../components/PipelineFilterPanel";
import PaginationBar from "../components/PaginationBar";
import { getRoute } from "../lib/routes";
import ActiveFiltersBar from "../components/ActiveFiltersBar";
import FilterToggleButton from "../components/FilterToggleButton";
import PageErrorState from "../components/PageErrorState";
import PermissionGate from "../components/PermissionGate";
import ImportPipelineDialog, { ImportUploadIcon } from "../components/ImportPipelineDialog";
import { pipelineStatusBadgeClass, pipelineStatusLabel, pipelineStatusShortLabel } from "../lib/badgeStyles";
import { dastOutcomeNarrative } from "../lib/dastNarrative";
import DastRunPanels, { DastRunChips } from "../components/DastRunPanels";
import {
  describePipelinesFilters,
  type PipelinesFilterChip,
  type PipelinesFilterState,
} from "../lib/pipelinesActiveFilters";
import { useFilterPanelOpen } from "../lib/useFilterPanelOpen";

const statusOptions = [
  { value: "all", label: "All statuses" },
  { value: "ADMITTED", label: "Admitted" },
  { value: "EXECUTING", label: "Executing" },
  { value: "UPLOADING_RESULTS", label: "Uploading Results" },
  { value: "FINDING_POSTPROCESSING", label: "Finding Post-processing" },
  { value: "WAITING_DEDUPLICATION_TO_FINISH", label: "Waiting Deduplication" },
  { value: "WAITING_CONFIRMATION_TO_PUSH_TO_AI", label: "Waiting AI Confirmation" },
  { value: "PUSH_TO_AI", label: "Push to AI" },
  { value: "WAITING_RESULT_FROM_AI", label: "Waiting AI Result" },
  { value: "FINISHED", label: "Finished" },
  { value: "FINISHED_WITH_WARNINGS", label: "Finished With Warnings" },
];

function formatDate(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString();
}

/** "3 days ago" for the phone row, where a full timestamp costs a line and says less. */
function formatAge(value?: string | null) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return formatDistanceToNowStrict(date, { addSuffix: true });
}

/**
 * Pipeline ids are 32 hex characters. The leading eight identify a run among the handful
 * on screen, which is all the collapsed row needs; the detail still shows the whole id.
 */
function shortPipelineId(id: string) {
  return id.length > 8 ? id.slice(0, 8) : id;
}

function truncateText(value: string | null | undefined, max = 32) {
  if (!value) return "—";
  if (value.length <= max) return value;
  return `${value.slice(0, max - 3)}...`;
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

/** Label above value, full width: a 32-character id or a long branch name needs the whole line. */
function DetailField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-[0.18em] text-slate-500">{label}</div>
      <div className="break-all text-xs text-slate-200">{children}</div>
    </div>
  );
}

function PipelineDetailCard({ pipeline }: { pipeline: PipelineSummary | null }) {
  if (!pipeline) {
    return <div className="text-sm text-slate-400">Select a pipeline to view details.</div>;
  }
  const dastOutcome = dastOutcomeNarrative(pipeline.dastOutcomeCode);
  return (
    <div className="space-y-4 text-xs text-slate-300">
      <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Pipeline Detail</div>
      {/* Identity and timing. Branch and commit live here and not in the collapsed row,
          so opening a pipeline adds information instead of repeating it. */}
      <div className="grid gap-3 rounded-xl border border-night-500 bg-night-900 px-4 py-3 sm:grid-cols-2">
        <div className="sm:col-span-2">
          <DetailField label="Pipeline">
            <span className="font-mono">{pipeline.id}</span>
          </DetailField>
        </div>
        <DetailField label="Branch">
          {pipeline.branch ? (
            <Link
              to={findingsPath({ project: pipeline.projectId, project_version: pipeline.branch })}
              className="aist-clickable-text"
              title={pipeline.branch}
            >
              {pipeline.branch}
            </Link>
          ) : (
            "—"
          )}
        </DetailField>
        <DetailField label="Commit">
          {pipeline.commit ? (
            <Link
              to={findingsPath({ project: pipeline.projectId, project_version: pipeline.commit })}
              className="aist-clickable-text font-mono"
              title={pipeline.commit}
            >
              {truncateText(pipeline.commit, 24)}
            </Link>
          ) : (
            "—"
          )}
        </DetailField>
        <DetailField label="Started">{formatDate(pipeline.started)}</DetailField>
        <DetailField label="Finished">{formatDate(pipeline.updated)}</DetailField>
      </div>

      {pipeline.executionType === "DAST" && dastOutcome ? (
        <div
          className={[
            "rounded-xl border px-4 py-3",
            dastOutcome.tone === "success"
              ? "border-emerald-400/30 bg-emerald-400/10"
              : dastOutcome.tone === "warning"
                ? "border-amber-400/30 bg-amber-400/10"
                : "border-night-500 bg-night-900",
          ].join(" ")}
        >
          <div className="font-semibold text-slate-100">{dastOutcome.title}</div>
          <div className="mt-1 text-slate-300">{dastOutcome.detail}</div>
        </div>
      ) : null}

      {pipeline.dastRun ? <DastRunPanels pipelineId={pipeline.id} /> : null}

      <div>
        <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Actions</div>
        <div className="mt-2">
          <ActionsBadge actions={pipeline.actions} />
        </div>
      </div>

      <Link
        to={findingsPath({ pipeline: pipeline.id, project: pipeline.projectId })}
        className="flex justify-center rounded-xl border border-night-500 px-3 py-2 text-xs text-slate-200 sm:inline-flex"
      >
        Open Findings
      </Link>
    </div>
  );
}

export default function PipelinesPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const projectsQuery = useProjects();
  const searchParams = useMemo(
    () => new URLSearchParams(location.search),
    [location.search],
  );
  const [selectedProjectId, setSelectedProjectId] = useState<number | undefined>();
  const [status, setStatus] = useState("all");
  const [search, setSearch] = useState("");
  const [createdFrom, setCreatedFrom] = useState("");
  const [createdTo, setCreatedTo] = useState("");
  const [pageSize, setPageSize] = useState<number>(50);
  const [pageIndex, setPageIndex] = useState<number>(0);
  const [expandedPipelineId, setExpandedPipelineId] = useState<string | null>(null);
  const [importDialogOpen, setImportDialogOpen] = useState(false);

  const projects = projectsQuery.data ?? [];
  useEffect(() => {
    if (!selectedProjectId) return;
    const exists = projects.some((project) => project.id === selectedProjectId);
    if (!exists) {
      setSelectedProjectId(undefined);
    }
  }, [projects, selectedProjectId]);

  useEffect(() => {
    const projectParam = searchParams.get("project") ?? searchParams.get("project_id");
    if (!projectParam) return;
    const parsed = Number(projectParam);
    if (!Number.isNaN(parsed)) {
      setSelectedProjectId(parsed);
    }
  }, [searchParams]);

  useEffect(() => {
    setCreatedFrom(searchParams.get("created_from") ?? searchParams.get("created_gte") ?? "");
    setCreatedTo(searchParams.get("created_to") ?? searchParams.get("created_lte") ?? "");
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
  const pageError = projectsQuery.error ?? pipelinesQuery.error;
  useEffect(() => {
    if (!expandedPipelineId || pipelines.find((item) => item.id === expandedPipelineId)) return;
    setExpandedPipelineId(null);
  }, [pipelines, expandedPipelineId]);

  const projectOptions = useMemo(
    () =>
      [
        { value: "all", label: "All projects" },
        ...projects.map((project) => ({
          value: String(project.id),
          label: project.name,
        })),
      ],
    [projects],
  );

  const summary = useMemo(() => {
    const byStatus: Record<string, number> = {};
    pipelines.forEach((item) => {
      byStatus[item.status] = (byStatus[item.status] ?? 0) + 1;
    });
    return {
      total: pipelines.length,
      finished: (byStatus.FINISHED ?? 0) + (byStatus.FINISHED_WITH_WARNINGS ?? 0),
      inProgress:
        (byStatus.ADMITTED ?? 0) +
        (byStatus.EXECUTING ?? 0) +
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

  const clearAllFilters = () => {
    setSelectedProjectId(undefined);
    setStatus("all");
    setSearch("");
    setCreatedFrom("");
    setCreatedTo("");
  };

  const filterChips = useMemo(
    () =>
      describePipelinesFilters(
        { projectId: selectedProjectId, status, search, createdFrom, createdTo },
        {
          projectName: (id) => projects.find((project) => project.id === id)?.name,
          statusLabel: (value) => statusOptions.find((option) => option.value === value)?.label,
        },
      ),
    [selectedProjectId, status, search, createdFrom, createdTo, projects],
  );

  // One chip removal is one state patch; the chip carries the patch, so this stays
  // ignorant of what any particular chip meant.
  const removeFilterChip = (chip: PipelinesFilterChip) => {
    const { patch } = chip;
    if ("projectId" in patch) setSelectedProjectId(patch.projectId);
    if (patch.status !== undefined) setStatus(patch.status);
    if (patch.search !== undefined) setSearch(patch.search);
    if (patch.createdFrom !== undefined) setCreatedFrom(patch.createdFrom);
    if (patch.createdTo !== undefined) setCreatedTo(patch.createdTo);
  };

  const { open: filterPanelOpen, toggle: toggleFilterPanel } = useFilterPanelOpen({
    page: "pipelines",
    hasFilters: filterChips.length > 0,
  });

  if (projectsQuery.isLoading) {
    return (
      <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
        Loading pipelines...
      </div>
    );
  }

  if (pageError) {
    return <PageErrorState error={pageError} fallbackTitle="Failed to load pipelines" />;
  }

  return (
    <div className={filterPanelOpen ? "grid min-h-0 gap-6 lg:grid-cols-[280px_minmax(0,1fr)]" : ""}>
      {filterPanelOpen ? (
        <div className="aist-scrollbar mb-4 self-start lg:sticky lg:top-24 lg:mb-0 lg:max-h-[calc(100vh-140px)] lg:overflow-auto">
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
            onClearAll={clearAllFilters}
          />
        </div>
      ) : null}

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
          <div className="flex flex-wrap items-center gap-2">
            <FilterToggleButton
              open={filterPanelOpen}
              onToggle={toggleFilterPanel}
              activeCount={filterChips.length}
            />
            <PermissionGate
              action="operate_project"
              organizationId={projects.find((project) => project.id === selectedProjectId)?.organizationId ?? undefined}
            >
              <button
                type="button"
                className="inline-flex items-center gap-2 rounded-xl bg-brand-600 px-4 py-2 text-sm font-medium text-night-900 transition hover:bg-brand-500"
                onClick={() => setImportDialogOpen(true)}
              >
                <ImportUploadIcon />
                Import pipeline launch
              </button>
            </PermissionGate>
          </div>
        </div>

        {filterPanelOpen ? null : (
          <ActiveFiltersBar chips={filterChips} onRemove={removeFilterChip} onClearAll={clearAllFilters} />
        )}

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
                  {/* One row, laid out for both widths. Below `md` it carries status, age,
                      product and a one-line meta strip and nothing else: the timestamps and
                      the commit belong to the detail, and no link sits inside the row, so the
                      whole card is a single tap target. */}
                  <div className="flex items-center justify-between gap-2">
                    <span
                      data-testid="pipeline-status-badge"
                      className={[
                        "rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-wide",
                        pipelineStatusBadgeClass(pipeline.status),
                      ].join(" ")}
                    >
                      {/* Only one of the two is in the accessibility tree at any width. */}
                      <span className="md:hidden">{pipelineStatusShortLabel(pipeline.status)}</span>
                      <span className="hidden md:inline">{pipelineStatusLabel(pipeline.status)}</span>
                    </span>
                    <div className="flex items-center gap-2 text-xs text-slate-400">
                      <span className="hidden md:inline">Pipeline {pipeline.id}</span>
                      <span className="md:hidden">{formatAge(pipeline.started) ?? "—"}</span>
                      <button
                        type="button"
                        className="aist-clickable-text hidden md:inline"
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

                  <div className="mt-2 md:hidden">
                    <div className="truncate text-sm font-semibold text-white">{pipeline.productName}</div>
                    <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-400">
                      <span className="font-mono">{shortPipelineId(pipeline.id)}</span>
                      {pipeline.branch ? (
                        <>
                          <span aria-hidden="true">·</span>
                          <span className="max-w-[10rem] truncate" title={pipeline.branch}>{pipeline.branch}</span>
                        </>
                      ) : null}
                      <span aria-hidden="true">·</span>
                      <span>{pipeline.findings} findings</span>
                    </div>
                  </div>

                  <div className="hidden md:block">
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
                    {pipeline.dastRun ? <DastRunChips run={pipeline.dastRun} /> : null}
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
      <ImportPipelineDialog open={importDialogOpen} onClose={() => setImportDialogOpen(false)} />
    </div>
  );
}
