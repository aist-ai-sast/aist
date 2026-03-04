import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import type { ProductSummary, RiskScore } from "../types";
import { useProductSummaries } from "../lib/queries";
import { getRoute } from "../lib/routes";
import MultiSelectChips from "../components/MultiSelectChips";
import SelectField from "../components/SelectField";
import TextInput from "../components/TextInput";
import FilterClearButton from "../components/FilterClearButton";
import PermissionGate from "../components/PermissionGate";
import PaginationBar from "../components/PaginationBar";
import PageErrorState from "../components/PageErrorState";
import { SEVERITY_ORDER, severityBarClass } from "../lib/badgeStyles";

const RISK_SCORE_META: Record<NonNullable<RiskScore["label"]>, { className: string }> = {
  critical: { className: "border-danger-500/40 bg-danger-500/10 text-danger-200" },
  high:     { className: "border-amber-400/40 bg-amber-400/10 text-amber-200" },
  medium:   { className: "border-yellow-500/30 bg-yellow-500/5 text-yellow-300" },
  low:      { className: "border-emerald-500/40 bg-emerald-500/10 text-emerald-200" },
};

const PIPELINE_STATUS_META: Record<string, { label: string; className: string }> = {
  finished:               { label: "Finished",   className: "border-emerald-500/40 bg-emerald-500/10 text-emerald-200" },
  finished_with_warnings: { label: "Warnings",   className: "border-amber-400/40 bg-amber-400/10 text-amber-200" },
  running:                { label: "Running",    className: "border-brand-500/40 bg-brand-500/10 text-brand-200" },
  failed:                 { label: "Failed",     className: "border-danger-500/40 bg-danger-500/10 text-danger-200" },
  pending:                { label: "Pending",    className: "border-night-500 bg-night-800 text-slate-400" },
  cancelled:              { label: "Cancelled",  className: "border-night-500 bg-night-800 text-slate-400" },
};

const sortOptions = [
  { value: "name_asc",     label: "Name A–Z" },
  { value: "active_desc",  label: "Most active findings" },
  { value: "critical_desc", label: "Most critical" },
  { value: "sync_desc",    label: "Recently synced" },
];

const statusOptions = [
  { value: "all",      label: "All statuses" },
  { value: "active",   label: "Active" },
  { value: "inactive", label: "Inactive" },
];

function formatRelativeTime(value?: string | null): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const diff = Date.now() - date.getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1)  return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return date.toLocaleDateString();
}

function sortSummaries(items: ProductSummary[], sort: string): ProductSummary[] {
  return [...items].sort((a, b) => {
    switch (sort) {
      case "name_asc":     return a.name.localeCompare(b.name);
      case "active_desc":  return b.findingsActive - a.findingsActive;
      case "critical_desc": return (b.severity.Critical ?? 0) - (a.severity.Critical ?? 0);
      case "sync_desc": {
        const aTime = a.lastPipeline?.updated ? new Date(a.lastPipeline.updated).getTime() : 0;
        const bTime = b.lastPipeline?.updated ? new Date(b.lastPipeline.updated).getTime() : 0;
        return bTime - aTime;
      }
      default: return 0;
    }
  });
}

function severityTotal(severity: ProductSummary["severity"]) {
  return Object.values(severity).reduce((sum, val) => sum + val, 0);
}

function SeverityBar({ severity }: { severity: ProductSummary["severity"] }) {
  const total = severityTotal(severity);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const segments = SEVERITY_ORDER.map((key) => ({ key, color: severityBarClass(key) }));
  return (
    <div className="relative">
      <div className="flex h-2 w-full overflow-hidden rounded-full bg-night-900">
        {segments.map((segment) => {
          const value = severity[segment.key] ?? 0;
          const width = total ? (value / total) * 100 : 0;
          return (
            <div
              key={segment.key}
              className={[
                segment.color,
                "transition-all",
                activeKey === segment.key ? "brightness-125 ring-2 ring-brand-500/60" : "",
              ].join(" ")}
              style={{ width: `${width}%` }}
              onMouseEnter={() => setActiveKey(segment.key)}
              onMouseLeave={() => setActiveKey(null)}
            />
          );
        })}
      </div>
      {activeKey ? (
        <div className="pointer-events-none absolute left-1/2 mt-1.5 -translate-x-1/2 whitespace-nowrap rounded-lg border border-night-500 bg-night-900 px-2.5 py-1 text-xs text-slate-200 shadow-panel">
          {activeKey}: {severity[activeKey as keyof typeof severity] ?? 0}
        </div>
      ) : null}
    </div>
  );
}

function SkeletonCard() {
  return (
    <div className="animate-pulse p-5 aist-card">
      <div className="flex items-center justify-between gap-2">
        <div className="h-5 w-2/3 rounded-lg bg-night-600" />
        <div className="h-5 w-14 rounded-full bg-night-600" />
      </div>
      <div className="mt-4 h-2 w-full rounded-full bg-night-600" />
      <div className="mt-2 flex gap-3">
        <div className="h-3 w-20 rounded bg-night-600" />
        <div className="h-3 w-20 rounded bg-night-600" />
      </div>
      <div className="mt-4 flex gap-2">
        <div className="h-7 w-24 rounded-xl bg-night-600" />
        <div className="h-7 w-24 rounded-xl bg-night-600" />
      </div>
    </div>
  );
}

export default function ProductsPage() {
  const navigate = useNavigate();
  const summariesQuery = useProductSummaries();
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const [sort, setSort] = useState("name_asc");
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [pageSize, setPageSize] = useState<number>(12);
  const [pageIndex, setPageIndex] = useState<number>(0);

  const summaries = summariesQuery.data ?? [];

  const tagOptions = useMemo(() => {
    const tags = new Set<string>();
    summaries.forEach((s) => s.tags.forEach((tag) => tags.add(tag)));
    return Array.from(tags).sort();
  }, [summaries]);

  const filtered = useMemo(() => {
    const base = summaries.filter((s) => {
      if (status !== "all" && s.status !== status) return false;
      if (search && !s.name.toLowerCase().includes(search.toLowerCase())) return false;
      if (selectedTags.length && !selectedTags.some((tag) => s.tags.includes(tag))) return false;
      return true;
    });
    return sortSummaries(base, sort);
  }, [summaries, status, search, selectedTags, sort]);

  const paged = useMemo(() => {
    const start = pageIndex * pageSize;
    return filtered.slice(start, start + pageSize);
  }, [filtered, pageIndex, pageSize]);

  useEffect(() => {
    setPageIndex(0);
  }, [status, search, selectedTags, sort, pageSize]);

  useEffect(() => {
    const maxPage = Math.max(0, Math.ceil(filtered.length / pageSize) - 1);
    if (pageIndex > maxPage) setPageIndex(maxPage);
  }, [filtered.length, pageIndex, pageSize]);

  const clearAllFilters = () => {
    setSearch("");
    setStatus("all");
    setSelectedTags([]);
  };

  const lastSync = useMemo(() => {
    const times = summaries
      .map((s) => s.lastPipeline?.updated)
      .filter(Boolean)
      .map((v) => new Date(v as string).getTime())
      .filter((v) => !Number.isNaN(v));
    if (!times.length) return null;
    return formatRelativeTime(new Date(Math.max(...times)).toISOString());
  }, [summaries]);

  if (summariesQuery.isError) {
    return <PageErrorState error={summariesQuery.error} fallbackTitle="Failed to load projects" />;
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Projects</div>
          <div className="mt-2 text-2xl font-semibold text-white">
            {summariesQuery.isLoading ? "—" : `${filtered.length} projects`}
          </div>
          {lastSync ? (
            <div className="mt-1 text-xs text-slate-400">Last sync: {lastSync}</div>
          ) : null}
        </div>
        <PermissionGate action="manage_access">
          <button className="rounded-xl bg-brand-500 px-4 py-2 text-xs font-semibold text-night-900">
            Manage access
          </button>
        </PermissionGate>
      </div>

      <div className="p-4 aist-card">
        <div className="mb-3 flex items-center justify-between gap-2">
          <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Filters</div>
          <FilterClearButton onClick={clearAllFilters} label="Clear all" />
        </div>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <div className="flex-1">
            <div className="mb-1 flex items-center justify-between gap-2">
              <label className="text-xs text-slate-400">Search</label>
              {search ? <FilterClearButton onClick={() => setSearch("")} /> : null}
            </div>
            <TextInput
              className="px-4"
              placeholder="Search projects..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <div className="w-full sm:w-44">
            <div className="mb-1 flex items-center justify-between gap-2">
              <label className="text-xs text-slate-400">Status</label>
              {status !== "all" ? <FilterClearButton onClick={() => setStatus("all")} /> : null}
            </div>
            <SelectField label="Status" value={status} onChange={setStatus} hideLabel options={statusOptions} />
          </div>
          <div className="w-full sm:w-48">
            <label className="mb-1 block text-xs text-slate-400">Sort by</label>
            <SelectField label="Sort" value={sort} onChange={setSort} hideLabel options={sortOptions} />
          </div>
        </div>
        <div className="mt-4 grid gap-4 md:grid-cols-2">
          <MultiSelectChips
            label="Tags"
            options={tagOptions}
            selected={selectedTags}
            onChange={setSelectedTags}
            onClear={() => setSelectedTags([])}
            visibleCount={8}
          />
        </div>
      </div>

      <div className="flex min-h-[calc(100vh-280px)] flex-col">
        {summariesQuery.isLoading ? (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {Array.from({ length: 6 }).map((_, i) => <SkeletonCard key={i} />)}
          </div>
        ) : filtered.length === 0 ? (
          <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
            No projects match the current filters.
          </div>
        ) : (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {paged.map((summary) => {
              const pipelineMeta = summary.lastPipeline?.status
                ? (PIPELINE_STATUS_META[summary.lastPipeline.status] ?? {
                    label: summary.lastPipeline.status.replace(/_/g, " "),
                    className: "border-night-500 bg-night-800 text-slate-400",
                  })
                : null;
              const relativeTime = formatRelativeTime(summary.lastPipeline?.updated);
              const visibleTags = summary.tags.slice(0, 3);
              const extraTagCount = summary.tags.length - visibleTags.length;

              return (
                <article
                  key={summary.productId}
                  className="p-5 aist-card aist-card--interactive"
                  role="button"
                  tabIndex={0}
                  aria-label={`Open findings for ${summary.name}`}
                  onClick={() =>
                    navigate(`${getRoute("ui_findings_path")}?${new URLSearchParams({ project: String(summary.projectId) })}`)
                  }
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      navigate(`${getRoute("ui_findings_path")}?${new URLSearchParams({ project: String(summary.projectId) })}`);
                    }
                  }}
                >
                  <div className="flex items-start justify-between gap-2">
                    <h3 className="text-base font-semibold leading-snug text-white">{summary.name}</h3>
                    <div className="flex shrink-0 items-center gap-1.5">
                      {summary.riskScore ? (
                        <span
                          className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${RISK_SCORE_META[summary.riskScore.label].className}`}
                          title={`Risk score: ${summary.riskScore.score}`}
                        >
                          {summary.riskScore.score}
                        </span>
                      ) : null}
                      <span
                        className={[
                          "rounded-full border px-2 py-0.5 text-[11px] font-medium capitalize",
                          summary.status === "active"
                            ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
                            : "border-night-500 bg-night-800 text-slate-400",
                        ].join(" ")}
                      >
                        {summary.status}
                      </span>
                    </div>
                  </div>

                  <div className="mt-3">
                    <SeverityBar severity={summary.severity} />
                    <div className="mt-2 flex flex-wrap gap-3 text-xs text-slate-400">
                      <span>Active: <span className="text-slate-200">{summary.findingsActive}</span></span>
                      <span>Total: <span className="text-slate-200">{summary.findingsTotal}</span></span>
                    </div>
                  </div>

                  {visibleTags.length > 0 ? (
                    <div className="mt-3 flex flex-wrap gap-1.5">
                      {visibleTags.map((tag) => (
                        <span key={tag} className="rounded-full border border-night-500 bg-night-800 px-2 py-0.5 text-[11px] text-slate-400">
                          {tag}
                        </span>
                      ))}
                      {extraTagCount > 0 ? (
                        <span className="rounded-full border border-night-500 bg-night-800 px-2 py-0.5 text-[11px] text-slate-500">
                          +{extraTagCount}
                        </span>
                      ) : null}
                    </div>
                  ) : null}

                  <div className="mt-3 flex items-center gap-2">
                    {pipelineMeta ? (
                      <span className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${pipelineMeta.className}`}>
                        {pipelineMeta.label}
                      </span>
                    ) : (
                      <span className="text-xs text-slate-500">No pipeline</span>
                    )}
                    {relativeTime ? (
                      <span className="text-xs text-slate-500">{relativeTime}</span>
                    ) : null}
                  </div>

                  <div className="mt-4 flex flex-wrap gap-2">
                    <Link
                      to={`${getRoute("ui_findings_path")}?${new URLSearchParams({ project: String(summary.projectId) })}`}
                      className="rounded-xl border border-night-500 px-3 py-2 text-xs text-slate-200 transition hover:border-brand-600/40"
                      onClick={(e) => e.stopPropagation()}
                    >
                      View findings
                    </Link>
                    <Link
                      to={`${getRoute("ui_pipelines_path")}?${new URLSearchParams({ project: String(summary.projectId) })}`}
                      className="rounded-xl border border-night-500 px-3 py-2 text-xs text-slate-200 transition hover:border-brand-600/40"
                      onClick={(e) => e.stopPropagation()}
                    >
                      View pipelines
                    </Link>
                  </div>
                </article>
              );
            })}
          </div>
        )}
        <div className="mt-auto">
          <PaginationBar
            count={filtered.length}
            noun="projects"
            pageIndex={pageIndex}
            pageSize={pageSize}
            onPageIndexChange={setPageIndex}
            onPageSizeChange={setPageSize}
            rowOptions={[12, 24, 50]}
          />
        </div>
      </div>
    </div>
  );
}
