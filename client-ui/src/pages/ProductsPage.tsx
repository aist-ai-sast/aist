import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import type { ProductSummary } from "../types";
import { useProductSummaries } from "../lib/queries";
import { getRoute } from "../lib/routes";
import MultiSelectChips from "../components/MultiSelectChips";
import SelectField from "../components/SelectField";
import PermissionGate from "../components/PermissionGate";
import PaginationBar from "../components/PaginationBar";

const statusOptions = [
  { value: "all", label: "All statuses" },
  { value: "active", label: "Active" },
  { value: "inactive", label: "Inactive" },
];

function formatLastSync(value?: string | null) {
  if (!value) return "No sync data";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "No sync data";
  return date.toLocaleString();
}

function formatLastPipeline(pipeline?: ProductSummary["lastPipeline"]) {
  if (!pipeline?.status) return "No pipeline data";
  return pipeline.status.replace(/_/g, " ");
}

function severityTotal(severity: ProductSummary["severity"]) {
  return Object.values(severity).reduce((sum, val) => sum + val, 0);
}

function SeverityBar({ severity }: { severity: ProductSummary["severity"] }) {
  const total = severityTotal(severity);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const activeValue = activeKey ? severity[activeKey as keyof typeof severity] ?? 0 : null;
  const segments = [
    { key: "Critical", color: "bg-danger-500" },
    { key: "High", color: "bg-danger-500/70" },
    { key: "Medium", color: "bg-amber-400" },
    { key: "Low", color: "bg-slate-400" },
    { key: "Info", color: "bg-slate-500" },
  ] as const;
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
        <div className="absolute right-0 mt-2 rounded-lg border border-night-500 bg-night-900 px-3 py-1 text-xs text-slate-200 shadow-panel">
          {activeKey}: {activeValue ?? 0}
        </div>
      ) : null}
    </div>
  );
}

export default function ProductsPage() {
  const navigate = useNavigate();
  const summariesQuery = useProductSummaries();
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [pageSize, setPageSize] = useState<number>(12);
  const [pageIndex, setPageIndex] = useState<number>(0);

  const summaries = summariesQuery.data ?? [];
  const tagOptions = useMemo(() => {
    const tags = new Set<string>();
    summaries.forEach((summary) => summary.tags.forEach((tag) => tags.add(tag)));
    return Array.from(tags).sort();
  }, [summaries]);

  const filtered = useMemo(() => {
    return summaries.filter((summary) => {
      if (status !== "all" && summary.status !== status) return false;
      if (search && !summary.name.toLowerCase().includes(search.toLowerCase())) return false;
      if (selectedTags.length && !selectedTags.some((tag) => summary.tags.includes(tag))) return false;
      return true;
    });
  }, [summaries, status, search, selectedTags]);

  const paged = useMemo(() => {
    const start = pageIndex * pageSize;
    return filtered.slice(start, start + pageSize);
  }, [filtered, pageIndex, pageSize]);

  useEffect(() => {
    setPageIndex(0);
  }, [status, search, selectedTags, pageSize]);

  useEffect(() => {
    const maxPage = Math.max(0, Math.ceil(filtered.length / pageSize) - 1);
    if (pageIndex > maxPage) {
      setPageIndex(maxPage);
    }
  }, [filtered.length, pageIndex, pageSize]);

  const lastSync = useMemo(() => {
    const dates = summaries
      .map((summary) => summary.lastSync)
      .filter(Boolean)
      .map((value) => new Date(value as string).getTime())
      .filter((value) => !Number.isNaN(value));
    if (!dates.length) return "No sync data";
    return new Date(Math.max(...dates)).toLocaleString();
  }, [summaries]);

  if (summariesQuery.isLoading) {
    return (
      <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
        Loading projects...
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Projects</div>
          <div className="mt-2 text-2xl font-semibold text-white">
            {filtered.length} projects
          </div>
          <div className="mt-1 text-xs text-slate-400">Last sync: {lastSync}</div>
        </div>
        <PermissionGate action="manage_access">
          <button className="rounded-xl bg-brand-500 px-4 py-2 text-xs font-semibold text-night-900">
            Manage access
          </button>
        </PermissionGate>
      </div>

      <div className="p-4 aist-card">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <input
            className="flex-1 rounded-xl border border-night-500 bg-night-600 px-4 py-2 text-sm text-white placeholder:text-slate-400"
            placeholder="Search projects..."
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <div className="w-full sm:w-44">
            <SelectField
              label="Status"
              value={status}
              onChange={setStatus}
              hideLabel
              options={statusOptions}
            />
          </div>
        </div>
        <div className="mt-4 grid gap-4 md:grid-cols-2">
          <MultiSelectChips
            label="Tags"
            options={tagOptions}
            selected={selectedTags}
            onChange={setSelectedTags}
            visibleCount={8}
          />
        </div>
      </div>

      <div className="flex min-h-[calc(100vh-280px)] flex-col">
        {filtered.length === 0 ? (
          <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
            No projects match the current filters.
          </div>
        ) : (
          <div className="space-y-4">
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              {paged.map((summary) => (
                <article
                  key={summary.productId}
                  className="p-5 aist-card aist-card--interactive"
                  role="button"
                  tabIndex={0}
                  aria-label={`Open findings for ${summary.name}`}
                  onClick={() =>
                    navigate(
                      `${getRoute("ui_findings_path")}?${new URLSearchParams({ project: String(summary.projectId) }).toString()}`,
                    )
                  }
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      navigate(
                        `${getRoute("ui_findings_path")}?${new URLSearchParams({ project: String(summary.projectId) }).toString()}`,
                      );
                    }
                  }}
                >
                  <div className="flex items-center justify-between">
                    <h3 className="text-lg font-semibold text-white">{summary.name}</h3>
                    <span className="text-xs text-slate-400 capitalize">{summary.status}</span>
                  </div>
                  <div className="mt-3">
                    <SeverityBar severity={summary.severity} />
                    <div className="mt-2 flex flex-wrap gap-2 text-xs text-slate-400">
                      <span>Active findings: {summary.findingsActive}</span>
                      <span>Total findings: {summary.findingsTotal}</span>
                    </div>
                  </div>
                  <div className="mt-4 flex items-center justify-between text-xs text-slate-400">
                    <span>Last pipeline: {formatLastPipeline(summary.lastPipeline)}</span>
                    <span>{formatLastSync(summary.lastPipeline?.updated)}</span>
                  </div>
                  <div className="mt-4 flex flex-wrap gap-2">
                    <Link
                      to={`${getRoute("ui_findings_path")}?${new URLSearchParams({ project: String(summary.projectId) }).toString()}`}
                      className="rounded-xl border border-night-500 px-3 py-2 text-xs text-slate-200"
                      onClick={(event) => event.stopPropagation()}
                    >
                      View findings
                    </Link>
                    <Link
                      to={`${getRoute("ui_pipelines_path")}?${new URLSearchParams({ project: String(summary.projectId) }).toString()}`}
                      className="rounded-xl border border-night-500 px-3 py-2 text-xs text-slate-200"
                      onClick={(event) => event.stopPropagation()}
                    >
                      View pipelines
                    </Link>
                  </div>
                </article>
              ))}
            </div>
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
            rowOptions={[6, 12, 24]}
          />
        </div>
      </div>
    </div>
  );
}
