import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import type { ProductSummary } from "../types";
import { useProductSummaries } from "../lib/queries";
import MultiSelectChips from "../components/MultiSelectChips";
import SelectField from "../components/SelectField";
import PermissionGate from "../components/PermissionGate";

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
  const summariesQuery = useProductSummaries();
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const [selectedRisk, setSelectedRisk] = useState<string[]>([]);
  const [selectedTags, setSelectedTags] = useState<string[]>([]);

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
      if (selectedRisk.length) {
        const hasRisk =
          (selectedRisk.includes("Risk Accepted") && summary.risk.riskAccepted > 0) ||
          (selectedRisk.includes("Under Review") && summary.risk.underReview > 0) ||
          (selectedRisk.includes("Mitigated") && summary.risk.mitigated > 0);
        if (!hasRisk) return false;
      }
      return true;
    });
  }, [summaries, status, search, selectedTags, selectedRisk]);

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
        Loading products...
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Products</div>
          <div className="mt-2 text-2xl font-semibold text-white">
            {filtered.length} products
          </div>
          <div className="mt-1 text-xs text-slate-400">Last sync: {lastSync}</div>
        </div>
        <PermissionGate action="manage_access">
          <button className="rounded-xl bg-brand-500 px-4 py-2 text-xs font-semibold text-night-900">
            Manage access
          </button>
        </PermissionGate>
      </div>

      <div className="rounded-2xl border border-night-500 bg-night-700 p-4">
        <div className="flex flex-wrap items-center gap-3">
          <input
            className="flex-1 rounded-xl border border-night-500 bg-night-600 px-4 py-2 text-sm text-white placeholder:text-slate-400"
            placeholder="Search products..."
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <div className="w-44">
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
            label="Risk State"
            options={["Risk Accepted", "Under Review", "Mitigated"]}
            selected={selectedRisk}
            onChange={setSelectedRisk}
            visibleCount={6}
          />
          <MultiSelectChips
            label="Tags"
            options={tagOptions}
            selected={selectedTags}
            onChange={setSelectedTags}
            visibleCount={8}
          />
        </div>
      </div>

      {filtered.length === 0 ? (
        <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
          No products match the current filters.
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {filtered.map((summary) => (
            <article
              key={summary.productId}
              className="rounded-2xl border border-night-500 bg-night-700 p-5 shadow-panel"
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
              <div className="mt-3 flex flex-wrap gap-2">
                {summary.risk.riskAccepted > 0 ? (
                  <span className="rounded-full border border-amber-400/40 bg-amber-400/10 px-3 py-1 text-xs text-amber-400">
                    Risk Accepted
                  </span>
                ) : null}
                {summary.risk.underReview > 0 ? (
                  <span className="rounded-full border border-amber-400/40 bg-amber-400/10 px-3 py-1 text-xs text-amber-400">
                    Under Review
                  </span>
                ) : null}
                {summary.risk.mitigated > 0 ? (
                  <span className="rounded-full border border-amber-400/40 bg-amber-400/10 px-3 py-1 text-xs text-amber-400">
                    Mitigated
                  </span>
                ) : null}
              </div>
              <div className="mt-4 flex items-center justify-between text-xs text-slate-400">
                <span>Last pipeline: {formatLastPipeline(summary.lastPipeline)}</span>
                <span>{formatLastSync(summary.lastPipeline?.updated)}</span>
              </div>
              <div className="mt-4 flex flex-wrap gap-2">
                <Link
                  to={`/?product=${summary.productId}`}
                  className="rounded-xl border border-night-500 px-3 py-2 text-xs text-slate-200"
                >
                  View findings
                </Link>
                <Link
                  to={`/pipelines?product=${summary.productId}`}
                  className="rounded-xl border border-night-500 px-3 py-2 text-xs text-slate-200"
                >
                  View pipelines
                </Link>
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
