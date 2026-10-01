import ReactECharts from "echarts-for-react";
import { type ReactNode, useCallback, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import ActiveFiltersBar from "../components/ActiveFiltersBar";
import FilterClearButton from "../components/FilterClearButton";
import FilterPanel from "../components/FilterPanel";
import PageErrorState from "../components/PageErrorState";
import { agingBucketRange, type DrilldownPatch, drilldownSearch, weekRange } from "../lib/dashboardDrilldown";
import { optionLabel, WORK_ITEM_STATUS_OPTIONS } from "../lib/findingsFilterOptions";
import type { TriageProgress, TriageState } from "../lib/queries";
import type { Severity } from "../types";
import { useDashboardSummary, useProjects } from "../lib/queries";
import { getRoute } from "../lib/routes";
import { ACCENT_SELECTED_CLASS } from "../lib/uiClasses";
import { useFindingsFilterState } from "../lib/useFindingsFilterState";

// ─── Constants ────────────────────────────────────────────────────────────────

const CHART_TEXT_COLOR = "#94a3b8";
const CHART_AXIS_COLOR = "rgba(45, 67, 105, 0.85)";

const SEVERITY_COLORS: Record<string, string> = {
  Critical: "#ff6b6b",
  High: "#fb923c",
  Medium: "#fbbf24",
  Low: "#94a3b8",
  Info: "#64748b",
};
const SEVERITY_KEYS = ["Critical", "High", "Medium", "Low", "Info"] as const;

const STATUS_COLORS: Record<string, string> = {
  active: "#4dd4ff",
  mitigated: "#34d399",
  risk_accepted: "#fb923c",
  under_review: "#a78bfa",
  false_positive: "#64748b",
  out_of_scope: "#475569",
};
const STATUS_KEYS = [
  "active",
  "mitigated",
  "risk_accepted",
  "under_review",
  "false_positive",
  "out_of_scope",
] as const;
const STATUS_LABELS: Record<string, string> = {
  active: "Active",
  mitigated: "Mitigated",
  risk_accepted: "Risk Accepted",
  under_review: "Under Review",
  false_positive: "False Positive",
  out_of_scope: "Out of Scope",
};

const AGE_BUCKET_LABELS: Record<string, string> = {
  "0_7": "0–7d",
  "8_30": "8–30d",
  "31_90": "31–90d",
  "90_plus": "90+d",
};

const VERDICT_COLORS: Record<string, string> = {
  true_positive: "#4dd4ff",
  false_positive: "#f87171",
  uncertain: "#fbbf24",
};
const VERDICT_LABELS: Record<string, string> = {
  true_positive: "True Positive",
  false_positive: "False Positive",
  uncertain: "Uncertain",
};

// Maps typed severity key → TopProject field name
const SEVERITY_FIELD_MAP: Record<
  (typeof SEVERITY_KEYS)[number],
  "critical" | "high" | "medium" | "low" | "info"
> = {
  Critical: "critical",
  High: "high",
  Medium: "medium",
  Low: "low",
  Info: "info",
};

const TOOLTIP_STYLE = {
  backgroundColor: "rgba(15,23,42,0.95)",
  borderColor: CHART_AXIS_COLOR,
  textStyle: { color: "#e2e8f0" },
  confine: true,
  className: "aist-chart-tooltip",
};

const WEEK_MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

// ─── Utilities ────────────────────────────────────────────────────────────────

function escapeTooltipText(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/** "2024-03-11" → "Mar 11" */
function formatWeekLabel(isoWeek: string): string {
  const parts = isoWeek.split("-");
  const month = parseInt(parts[1] ?? "1", 10) - 1;
  const day = parseInt(parts[2] ?? "1", 10);
  return `${WEEK_MONTHS[month] ?? ""} ${day}`;
}

function getCweAccentColors(): string[] {
  const fallback = ["#4dd4ff", "#34d399", "#fbbf24", "#fb923c", "#a78bfa", "#64748b"];
  if (typeof window === "undefined") return fallback;
  const root = getComputedStyle(document.documentElement);
  const colors = [
    "--aist-chart-accent-1",
    "--aist-chart-accent-2",
    "--aist-chart-accent-3",
    "--aist-chart-accent-4",
    "--aist-chart-accent-5",
    "--aist-chart-accent-6",
  ]
    .map((v) => root.getPropertyValue(v).trim())
    .filter(Boolean);
  return colors.length > 0 ? colors : fallback;
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function KpiCard({
  label,
  value,
  accent,
  hint,
  to,
  alert = false,
}: {
  label: string;
  value: number | string;
  accent?: string;
  hint?: ReactNode;
  to?: string;
  alert?: boolean;
}) {
  const className = [
    "block rounded-2xl border p-4",
    alert ? "border-danger-500/50 bg-danger-500/10" : "border-night-500 bg-night-700/90",
    to ? "transition-colors hover:border-brand-500/60 focus-visible:border-brand-500 focus-visible:outline-none" : "",
  ].join(" ");
  const body = (
    <>
      <div className="text-xs uppercase tracking-[0.16em] text-slate-400">{label}</div>
      <div className={["mt-2 text-3xl font-bold", accent ?? "text-slate-100"].join(" ")}>
        {typeof value === "number" ? value.toLocaleString() : value}
      </div>
      {hint ? <div className="mt-1.5 text-xs text-slate-400">{hint}</div> : null}
    </>
  );
  return to ? (
    <Link to={to} className={className}>
      {body}
    </Link>
  ) : (
    <div className={className}>{body}</div>
  );
}

const TRIAGE_STATE_COLORS: Record<TriageState, string> = {
  none: "rgba(100,116,139,0.16)",
  OPEN: "#fbbf24",
  IN_PROGRESS: "#4dd4ff",
  DONE: "#34d399",
  CANCELLED: "#64748b",
  UNKNOWN: "#475569",
};

// Untriaged work is drawn as an empty, hatched track: what is filled is done.
const UNTRIAGED_DECAL = {
  symbol: "rect",
  symbolSize: 1,
  dashArrayX: [1, 0],
  dashArrayY: [2, 5],
  rotation: -Math.PI / 4,
  color: "rgba(148,163,184,0.35)",
};

function triageStateLabel(state: TriageState): string {
  return optionLabel(WORK_ITEM_STATUS_OPTIONS, state);
}

function stateCount(triage: TriageProgress, state: TriageState): number {
  return triage.by_state.find((row) => row.state === state)?.count ?? 0;
}

function TicketHealthRow({
  title,
  detail,
  count,
  pill,
  pillClassName,
  to,
}: {
  title: string;
  detail: string;
  count: number;
  pill: string;
  pillClassName: string;
  to: string;
}) {
  return (
    <Link
      to={to}
      className="flex items-center justify-between gap-3 rounded-xl border border-night-500 bg-night-600/60 px-3 py-2.5 transition-colors hover:border-brand-500/60 focus-visible:border-brand-500 focus-visible:outline-none"
    >
      <span className="min-w-0">
        <span className="block text-sm text-slate-200">{title}</span>
        <span className="block text-xs text-slate-400">{detail}</span>
      </span>
      <span className="flex shrink-0 flex-col items-end gap-1">
        <span className="text-lg font-bold tabular-nums text-slate-100">{count.toLocaleString()}</span>
        <span
          className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.08em] ${pillClassName}`}
        >
          {pill}
        </span>
      </span>
    </Link>
  );
}

/**
 * Triage progress of the selected findings. Every number, share and target
 * comes from the summary; a click opens the Findings filter that reproduces it.
 */
function TriageProgressSection({
  triage,
  findingsLink,
  notApplicable,
}: {
  triage: TriageProgress;
  findingsLink: (patch: DrilldownPatch) => string;
  notApplicable?: ReactNode;
}) {
  const navigate = useNavigate();
  const recent = triage.ticketed_recently;

  const stateOption = useMemo(
    () => ({
      backgroundColor: "transparent",
      tooltip: { trigger: "item", ...TOOLTIP_STYLE },
      legend: { textStyle: { color: CHART_TEXT_COLOR }, itemWidth: 10, itemHeight: 10 },
      grid: { left: "2%", right: "2%", top: "40px", bottom: "16px", containLabel: true },
      xAxis: {
        type: "value",
        max: Math.max(triage.total_active, 1),
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        splitLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        axisLabel: { color: CHART_TEXT_COLOR },
      },
      yAxis: {
        type: "category",
        data: ["Active"],
        axisLabel: { color: CHART_TEXT_COLOR },
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
      },
      series: triage.by_state.map((row) => ({
        name: triageStateLabel(row.state),
        type: "bar",
        stack: "total",
        barMaxWidth: 32,
        cursor: "pointer",
        itemStyle: {
          color: TRIAGE_STATE_COLORS[row.state],
          ...(row.state === "none" ? { decal: UNTRIAGED_DECAL } : {}),
        },
        data: [{ value: row.count, state: row.state }],
      })),
    }),
    [triage],
  );

  const severityOption = useMemo(
    () => ({
      backgroundColor: "transparent",
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, ...TOOLTIP_STYLE },
      legend: { textStyle: { color: CHART_TEXT_COLOR }, itemWidth: 10, itemHeight: 10 },
      grid: { left: "2%", right: "2%", top: "40px", bottom: "8px", containLabel: true },
      xAxis: {
        type: "value",
        max: 100,
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        splitLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        axisLabel: { color: CHART_TEXT_COLOR, formatter: "{value}%" },
      },
      yAxis: [
        {
          type: "category",
          inverse: true,
          data: triage.by_severity.map((row) => row.severity),
          axisLabel: { color: CHART_TEXT_COLOR },
          axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        },
        {
          type: "category",
          inverse: true,
          position: "right",
          data: triage.by_severity.map((row) => `${row.ticketed} / ${row.total} · ${row.ticketed_pct}%`),
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: {
            color: (_value: string, index: number) =>
              triage.by_severity[index]?.below_target ? SEVERITY_COLORS.Critical : CHART_TEXT_COLOR,
          },
        },
      ],
      series: [
        {
          name: "Ticketed",
          type: "bar",
          stack: "share",
          barMaxWidth: 14,
          cursor: "pointer",
          itemStyle: { color: TRIAGE_STATE_COLORS.IN_PROGRESS },
          data: triage.by_severity.map((row) => ({ value: row.ticketed_pct, severity: row.severity, state: "any" })),
        },
        {
          name: triageStateLabel("none"),
          type: "bar",
          stack: "share",
          barMaxWidth: 14,
          cursor: "pointer",
          itemStyle: { color: TRIAGE_STATE_COLORS.none, decal: UNTRIAGED_DECAL },
          data: triage.by_severity.map((row) => ({ value: row.untriaged_pct, severity: row.severity, state: "none" })),
        },
        {
          name: "Target",
          type: "scatter",
          symbol: "rect",
          symbolSize: [3, 22],
          itemStyle: { color: "#e2e8f0" },
          tooltip: { valueFormatter: (value: number) => `${value}%` },
          data: triage.by_severity.map((row) => (row.target_pct > 0 ? row.target_pct : null)),
        },
      ],
    }),
    [triage],
  );

  const throughputOption = useMemo(
    () => ({
      backgroundColor: "transparent",
      tooltip: { trigger: "axis", ...TOOLTIP_STYLE },
      legend: { textStyle: { color: CHART_TEXT_COLOR } },
      grid: { left: "3%", right: "3%", bottom: "10%", top: "42px", containLabel: true },
      xAxis: {
        type: "category",
        data: triage.weekly.map((row) => formatWeekLabel(row.week)),
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        axisLabel: { color: CHART_TEXT_COLOR, rotate: triage.weekly.length > 8 ? 30 : 0 },
      },
      yAxis: [
        {
          type: "value",
          name: "Findings",
          nameTextStyle: { color: CHART_TEXT_COLOR },
          axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
          splitLine: { lineStyle: { color: CHART_AXIS_COLOR } },
          axisLabel: { color: CHART_TEXT_COLOR },
          minInterval: 1,
        },
        {
          type: "value",
          name: "Backlog",
          nameTextStyle: { color: CHART_TEXT_COLOR },
          axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
          splitLine: { show: false },
          axisLabel: { color: CHART_TEXT_COLOR },
          minInterval: 1,
        },
      ],
      series: [
        {
          name: "Ticketed",
          type: "bar",
          stack: "decisions",
          barMaxWidth: 24,
          cursor: "pointer",
          itemStyle: { color: TRIAGE_STATE_COLORS.IN_PROGRESS },
          data: triage.weekly.map((row) => row.ticketed),
        },
        {
          name: "Dismissed",
          type: "bar",
          stack: "decisions",
          barMaxWidth: 24,
          itemStyle: { color: "#64748b", borderRadius: [3, 3, 0, 0] },
          data: triage.weekly.map((row) => row.dismissed),
        },
        {
          name: "New findings",
          type: "line",
          smooth: true,
          cursor: "pointer",
          itemStyle: { color: "#fbbf24" },
          data: triage.weekly.map((row) => row.new_findings),
        },
        {
          name: "Untriaged backlog",
          type: "line",
          yAxisIndex: 1,
          smooth: true,
          lineStyle: { type: "dashed" },
          itemStyle: { color: "#f87171" },
          data: triage.weekly.map((row) => row.untriaged_at_week_end),
        },
      ],
    }),
    [triage],
  );

  const throughputCard = (
    <ChartCard
      title="Triage Throughput"
      subtitle="Weekly triage decisions vs new findings · dashed line = untriaged backlog at week end · click to filter"
    >
      <ReactECharts
        option={throughputOption}
        style={{ width: "100%", height: "280px" }}
        opts={{ renderer: "svg" }}
        onEvents={{
          click: (params: { dataIndex?: number; seriesName?: string }) => {
            const row = triage.weekly[params.dataIndex ?? -1];
            if (!row) return;
            if (params.seriesName === "Ticketed") {
              navigate(findingsLink({ workItemLinkedFrom: row.week, workItemLinkedTo: row.week_end }));
            }
            if (params.seriesName === "New findings") {
              navigate(findingsLink({ createdFrom: row.week, createdTo: row.week_end }));
            }
          },
        }}
      />
    </ChartCard>
  );

  if (notApplicable) {
    return (
      <div className="space-y-4">
        <ChartCard title="Triage Progress" subtitle="Active findings by work item state">
          {notApplicable}
        </ChartCard>
        {throughputCard}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <KpiCard
          label="Untriaged Critical & High"
          value={triage.untriaged_critical_high}
          accent={triage.untriaged_critical_high > 0 ? "text-danger-500" : undefined}
          alert={triage.untriaged_critical_high > 0}
          hint="Active, no work item yet"
          to={findingsLink({ severities: ["Critical", "High"], status: "Active", workItemStatus: "none" })}
        />
        <KpiCard
          label="Triage Coverage"
          value={`${triage.coverage_pct}%`}
          hint={`${triage.ticketed.toLocaleString()} of ${triage.total_active.toLocaleString()} active findings ticketed`}
          to={findingsLink({ status: "Active", workItemStatus: "any" })}
        />
        <KpiCard
          label={`Ticketed · ${recent.days} days`}
          value={recent.count}
          hint={
            recent.delta_pct === null
              ? `None in the previous ${recent.days} days`
              : `${recent.delta_pct >= 0 ? "▲" : "▼"} ${Math.abs(recent.delta_pct)}% vs previous ${recent.days} days`
          }
          to={findingsLink({ workItemLinkedFrom: recent.linked_from, workItemLinkedTo: "" })}
        />
      </div>

      <ChartCard
        title="Triage Progress"
        subtitle="Active findings by work item state · a finding with several tickets counts once · click to filter"
      >
        <ReactECharts
          option={stateOption}
          style={{ width: "100%", height: "120px" }}
          opts={{ renderer: "svg" }}
          onEvents={{
            click: (params: { data?: { state?: TriageState } }) => {
              const state = params.data?.state;
              if (state) navigate(findingsLink({ status: "Active", workItemStatus: state }));
            },
          }}
        />
        <ReactECharts
          option={severityOption}
          style={{ width: "100%", height: "220px" }}
          opts={{ renderer: "svg" }}
          onEvents={{
            click: (params: { data?: { severity?: Severity; state?: "any" | "none" } }) => {
              const { severity, state } = params.data ?? {};
              if (!severity || !state) return;
              navigate(findingsLink({ severities: [severity], status: "Active", workItemStatus: state }));
            },
          }}
        />
      </ChartCard>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        {throughputCard}
        <ChartCard title="Ticket Health" subtitle="Ticketed findings that need a follow-up · click to filter">
          <div className="space-y-2.5">
            <TicketHealthRow
              title={`Open for more than ${triage.stale_open.days} days`}
              detail="Ticket created but nobody picked it up"
              count={triage.stale_open.count}
              pill="Stale"
              pillClassName="border-amber-400/40 bg-amber-400/10 text-amber-400"
              to={findingsLink({
                status: "Active",
                workItemStatus: "OPEN",
                workItemLinkedFrom: "",
                workItemLinkedTo: triage.stale_open.linked_until,
              })}
            />
            <TicketHealthRow
              title="Done in tracker, still detected"
              detail="The latest scan still reports the finding"
              count={stateCount(triage, "DONE")}
              pill="Verify"
              pillClassName="border-danger-500/40 bg-danger-500/10 text-danger-500"
              to={findingsLink({ status: "Active", workItemStatus: "DONE" })}
            />
            <TicketHealthRow
              title="Status unknown"
              detail="Manual link or tracker sync failed"
              count={stateCount(triage, "UNKNOWN")}
              pill="Sync"
              pillClassName="border-night-500 bg-night-600 text-slate-400"
              to={findingsLink({ status: "Active", workItemStatus: "UNKNOWN" })}
            />
          </div>
        </ChartCard>
      </div>
    </div>
  );
}

function ChartCard({
  title,
  subtitle,
  badge,
  children,
}: {
  title: string;
  subtitle?: string;
  badge?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="rounded-2xl border border-night-500 bg-night-700/80 p-4">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-semibold text-slate-200">{title}</p>
          {subtitle && <p className="mt-0.5 text-xs text-slate-400">{subtitle}</p>}
        </div>
        {badge}
      </div>
      {children}
    </div>
  );
}

/** Marks a widget the finding filter does not reach, so an unchanged chart is not read as a filter bug. */
function ScopeBadge({ label, hint }: { label: string; hint: string }) {
  return (
    <span
      title={hint}
      className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-night-500 bg-night-600 px-2.5 py-0.5 text-[11px] text-slate-400"
    >
      <svg viewBox="0 0 24 24" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <circle cx="12" cy="12" r="9" />
        <path d="M12 8v5M12 16v.5" />
      </svg>
      {label}
      <span className="sr-only">: {hint}</span>
    </span>
  );
}

/** Shown instead of an active-findings chart when the Status filter excludes active findings. */
function ChartNotApplicable({ height = 280, onResetStatus }: { height?: number; onResetStatus: () => void }) {
  return (
    <div
      className="flex flex-col items-center justify-center gap-2 px-6 text-center"
      style={{ height }}
    >
      <p className="text-sm text-slate-300">Not applicable to the current filters</p>
      <p className="text-xs text-slate-400">
        This chart counts active findings only; Status is set to Non-Active.
      </p>
      <FilterClearButton label="Reset Status" onClick={onResetStatus} />
    </div>
  );
}

function ChartEmpty({ height = 280 }: { height?: number }) {
  return (
    <div
      className="flex items-center justify-center text-sm text-slate-400"
      style={{ height }}
    >
      No data available
    </div>
  );
}

function DashboardSkeleton() {
  return (
    <div className="animate-pulse space-y-4">
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="h-[320px] rounded-2xl bg-night-700/50" />
        <div className="h-[320px] rounded-2xl bg-night-700/50" />
      </div>
      <div className="h-[140px] rounded-2xl bg-night-700/50" />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="h-[360px] rounded-2xl bg-night-700/50" />
        <div className="h-[360px] rounded-2xl bg-night-700/50" />
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="h-[360px] rounded-2xl bg-night-700/50" />
        <div className="h-[420px] rounded-2xl bg-night-700/50" />
      </div>
    </div>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────

const FILTER_PANEL_STORAGE_KEY = "aist.dashboard.filtersOpen";

function readStoredPanelOpen(): boolean {
  try {
    return window.localStorage.getItem(FILTER_PANEL_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function storePanelOpen(open: boolean) {
  try {
    window.localStorage.setItem(FILTER_PANEL_STORAGE_KEY, open ? "1" : "0");
  } catch {
    // Storage blocked (private mode): the panel state is a per-visit convenience.
  }
}

export default function DashboardPage() {
  const navigate = useNavigate();
  const projectsQuery = useProjects();
  const projects = useMemo(() => projectsQuery.data ?? [], [projectsQuery.data]);
  const filters = useFindingsFilterState({ projects });
  const dashboard = useDashboardSummary(filters.debounced);
  const hasFilters = filters.chips.length > 0;
  // Status "Non-Active" empties every chart that counts active findings only.
  const activeChartsNotApplicable = filters.debounced.status === "Non-Active";
  const resetStatus = useCallback(() => filters.apply({ status: "All" }), [filters]);

  // A shared link with filters opens the panel; otherwise the viewer's last choice.
  const [filterPanelOpen, setFilterPanelOpen] = useState<boolean>(() => hasFilters || readStoredPanelOpen());
  const toggleFilterPanel = () => {
    storePanelOpen(!filterPanelOpen);
    setFilterPanelOpen(!filterPanelOpen);
  };

  // Stable reference: new Date() once per mount
  const now = useMemo(() => new Date(), []);

  // CSS var colours read once per mount (vars don't change in runtime)
  const cweAccentColors = useMemo(getCweAccentColors, []);

  // Drill-down keeps the dashboard's whole filter; the click replaces only its own field.
  const buildFindingsLink = useCallback(
    (patch: DrilldownPatch = {}) => {
      const search = drilldownSearch(filters.debounced, patch);
      return `${getRoute("ui_findings_path")}${search ? `?${search}` : ""}`;
    },
    [filters.debounced],
  );
  const buildActiveFindingsLink = useCallback(
    (patch: DrilldownPatch) => buildFindingsLink({ status: "Active", ...patch }),
    [buildFindingsLink],
  );

  // ─── Raw data ──────────────────────────────────────────────────────────────
  const kpi = dashboard.data?.kpi;
  const severityDist = dashboard.data?.severity_distribution ?? {};
  const topProjects = dashboard.data?.top_projects ?? [];
  const statusBreakdown = dashboard.data?.finding_status_breakdown ?? {};
  const agingHeatmap = dashboard.data?.findings_aging_heatmap;
  const riskTrend = dashboard.data?.risk_trend ?? [];
  const cweDistribution = dashboard.data?.cwe_distribution ?? [];
  const aiAnalytics = dashboard.data?.ai_verdict_analytics;
  const pipelineTrend = dashboard.data?.pipeline_performance_trend ?? [];

  // Derived — kept outside useMemo because also used in event handlers
  const agingBuckets = agingHeatmap?.buckets ?? [];
  const agingSeverities = agingHeatmap?.severities ?? [];

  // ─── Derived data (memoized) ───────────────────────────────────────────────

  const sortedCweData = useMemo(
    () => [...cweDistribution].sort((a, b) => b.count - a.count),
    [cweDistribution],
  );

  const agingData = useMemo(
    () =>
      agingSeverities.flatMap((severity, sIdx) =>
        agingBuckets.map((bucket, bIdx) => [
          bIdx,
          sIdx,
          agingHeatmap?.matrix?.[severity]?.[bucket] ?? 0,
        ]),
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [agingHeatmap],
  );

  // ─── Chart options (memoized) ──────────────────────────────────────────────

  const severityDonutOption = useMemo(() => {
    const total = SEVERITY_KEYS.reduce((sum, k) => sum + (severityDist[k] ?? 0), 0);
    return {
      backgroundColor: "transparent",
      tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)", ...TOOLTIP_STYLE },
      legend: {
        bottom: "0%",
        textStyle: { color: CHART_TEXT_COLOR },
        itemWidth: 10,
        itemHeight: 10,
      },
      title: {
        text: total.toLocaleString(),
        subtext: "active",
        left: "50%",
        top: "38%",
        textAlign: "center",
        textStyle: { color: "#e2e8f0", fontSize: 20, fontWeight: "bold" },
        subtextStyle: { color: "#94a3b8", fontSize: 11 },
      },
      series: [
        {
          type: "pie",
          radius: ["52%", "72%"],
          center: ["50%", "48%"],
          data: SEVERITY_KEYS.map((k) => ({
            name: k,
            value: severityDist[k] ?? 0,
            itemStyle: { color: SEVERITY_COLORS[k] },
          })),
          emphasis: { scale: true },
          label: { show: false },
          cursor: "pointer",
        },
      ],
    };
  }, [severityDist]);

  const topProjectsOption = useMemo(
    () => ({
      backgroundColor: "transparent",
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, ...TOOLTIP_STYLE },
      legend: {
        textStyle: { color: CHART_TEXT_COLOR },
        itemWidth: 10,
        itemHeight: 10,
        top: 0,
      },
      grid: { left: "4%", right: "4%", bottom: "4%", top: "40px", containLabel: true },
      xAxis: {
        type: "value",
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        splitLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        axisLabel: { color: CHART_TEXT_COLOR },
      },
      yAxis: {
        type: "category",
        data: topProjects.map((p) => p.name),
        axisLabel: { color: CHART_TEXT_COLOR, width: 120, overflow: "truncate" },
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
      },
      ...(topProjects.length > 5
        ? { dataZoom: [{ type: "slider", yAxisIndex: 0, show: true, startValue: 0, endValue: 4 }] }
        : {}),
      series: SEVERITY_KEYS.map((k) => ({
        name: k,
        type: "bar",
        stack: "total",
        itemStyle: { color: SEVERITY_COLORS[k] },
        data: topProjects.map((p) => p[SEVERITY_FIELD_MAP[k]]),
      })),
    }),
    [topProjects],
  );

  const statusBreakdownOption = useMemo(
    () => ({
      backgroundColor: "transparent",
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, ...TOOLTIP_STYLE },
      legend: {
        textStyle: { color: CHART_TEXT_COLOR },
        itemWidth: 10,
        itemHeight: 10,
      },
      grid: { left: "2%", right: "2%", top: "40px", bottom: "16px", containLabel: true },
      xAxis: {
        type: "value",
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        splitLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        axisLabel: { color: CHART_TEXT_COLOR },
      },
      yAxis: {
        type: "category",
        data: ["Findings"],
        axisLabel: { color: CHART_TEXT_COLOR },
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
      },
      series: STATUS_KEYS.map((k) => ({
        name: STATUS_LABELS[k],
        type: "bar",
        stack: "total",
        barMaxWidth: 32,
        itemStyle: { color: STATUS_COLORS[k] },
        data: [statusBreakdown[k] ?? 0],
      })),
    }),
    [statusBreakdown],
  );

  const agingHeatmapOption = useMemo(
    () => ({
      backgroundColor: "transparent",
      tooltip: {
        position: "top",
        formatter: (params: { value?: [number, number, number] }) => {
          const v = params.value ?? [0, 0, 0];
          const bucket = agingBuckets[v[0]] ?? "";
          const severity = agingSeverities[v[1]] ?? "";
          return `${severity} · ${AGE_BUCKET_LABELS[bucket] ?? bucket}: ${v[2]}`;
        },
        ...TOOLTIP_STYLE,
      },
      grid: { left: "4%", right: "4%", bottom: "14%", top: "8%", containLabel: true },
      xAxis: {
        type: "category",
        data: agingBuckets.map((b) => AGE_BUCKET_LABELS[b] ?? b),
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        axisLabel: { color: CHART_TEXT_COLOR },
      },
      yAxis: {
        type: "category",
        data: agingSeverities,
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        axisLabel: { color: CHART_TEXT_COLOR },
      },
      visualMap: {
        min: 0,
        max: Math.max(...agingData.map((item) => Number(item[2])), 1),
        calculable: true,
        orient: "horizontal",
        left: "center",
        bottom: "0%",
        inRange: { color: ["#1e293b", "#0ea5e9"] },
        textStyle: { color: CHART_TEXT_COLOR },
      },
      series: [
        {
          type: "heatmap",
          data: agingData,
          label: { show: true, color: "#e2e8f0" },
          emphasis: { itemStyle: { shadowBlur: 10, shadowColor: "rgba(0,0,0,0.45)" } },
        },
      ],
    }),
    // agingBuckets/agingSeverities are derived from agingHeatmap — list agingHeatmap as dep
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [agingData, agingHeatmap],
  );

  const riskTrendOption = useMemo(
    () => ({
      backgroundColor: "transparent",
      tooltip: { trigger: "axis", ...TOOLTIP_STYLE },
      legend: { textStyle: { color: CHART_TEXT_COLOR } },
      grid: { left: "3%", right: "3%", bottom: "10%", top: "42px", containLabel: true },
      xAxis: {
        type: "category",
        data: riskTrend.map((item) => formatWeekLabel(item.week)),
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        axisLabel: { color: CHART_TEXT_COLOR, rotate: riskTrend.length > 8 ? 30 : 0 },
      },
      yAxis: {
        type: "value",
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        splitLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        axisLabel: { color: CHART_TEXT_COLOR },
      },
      series: [
        {
          name: "New",
          type: "line",
          smooth: true,
          data: riskTrend.map((item) => item.new_findings),
          itemStyle: { color: "#4dd4ff" },
        },
        {
          name: "Mitigated",
          type: "line",
          smooth: true,
          data: riskTrend.map((item) => item.mitigated_findings),
          itemStyle: { color: "#34d399" },
        },
        {
          name: "Net Delta",
          type: "bar",
          barMaxWidth: 24,
          data: riskTrend.map((item) => item.net),
          // Red when backlog is growing, green when shrinking
          itemStyle: {
            color: (params: { value: number }) =>
              params.value >= 0 ? "#f87171" : "#34d399",
          },
        },
      ],
    }),
    [riskTrend],
  );

  const cweBarOption = useMemo(
    () => ({
      backgroundColor: "transparent",
      tooltip: {
        trigger: "item",
        formatter: (params: { data?: Record<string, unknown> }) => {
          const d = params.data ?? {};
          const cwe = Number(d.cwe ?? 0);
          const count = Number(d.count ?? 0);
          const title = String(d.title ?? "");
          const description = String(d.description ?? "");
          const impact = String(d.impact ?? "");
          const header = `CWE-${cwe}${title ? ` — ${escapeTooltipText(title)}` : ""}`;
          return [
            `<div class="aist-tooltip-title">${header}</div>`,
            `<div class="aist-tooltip-row"><span class="aist-tooltip-label">Findings:</span> ${count.toLocaleString()}</div>`,
            description
              ? `<div class="aist-tooltip-row"><span class="aist-tooltip-label">Description:</span> ${escapeTooltipText(description)}</div>`
              : "",
            impact
              ? `<div class="aist-tooltip-row"><span class="aist-tooltip-label">Impact:</span> ${escapeTooltipText(impact)}</div>`
              : "",
          ]
            .filter(Boolean)
            .join("");
        },
        ...TOOLTIP_STYLE,
      },
      grid: { left: "1%", right: "4%", top: "4px", bottom: "4px", containLabel: true },
      xAxis: {
        type: "value",
        axisLabel: { color: CHART_TEXT_COLOR },
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        splitLine: { lineStyle: { color: CHART_AXIS_COLOR, type: "dashed" } },
        minInterval: 1,
      },
      yAxis: {
        type: "category",
        data: sortedCweData.map((item) =>
          item.title ? `CWE-${item.cwe}: ${item.title}` : `CWE-${item.cwe}`,
        ),
        inverse: true,
        axisLabel: { color: CHART_TEXT_COLOR, width: 140, overflow: "truncate", fontSize: 11 },
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
      },
      ...(sortedCweData.length > 8
        ? {
            dataZoom: [
              {
                type: "slider",
                yAxisIndex: 0,
                startValue: 0,
                endValue: 7,
                width: 14,
                right: 4,
                fillerColor: "rgba(77,212,255,0.10)",
                borderColor: CHART_AXIS_COLOR,
                handleStyle: { color: "#4dd4ff" },
                textStyle: { color: "transparent" },
              },
            ],
          }
        : {}),
      series: [
        {
          type: "bar",
          barMaxWidth: 20,
          data: sortedCweData.map((item, idx) => ({
            value: item.count,
            cwe: item.cwe,
            count: item.count,
            title: item.title,
            description: item.description,
            impact: item.impact,
            url: item.url,
            itemStyle: {
              color: cweAccentColors[idx % cweAccentColors.length],
              borderRadius: [0, 4, 4, 0],
            },
          })),
          label: {
            show: true,
            position: "right",
            color: CHART_TEXT_COLOR,
            fontSize: 11,
            formatter: (params: { value?: number }) =>
              String((params.value ?? 0).toLocaleString()),
          },
          emphasis: {
            itemStyle: { shadowBlur: 8, shadowColor: "rgba(77,212,255,0.3)" },
          },
          cursor: "pointer",
        },
      ],
    }),
    [sortedCweData, cweAccentColors],
  );

  const aiVerdictOption = useMemo(
    () => ({
      backgroundColor: "transparent",
      tooltip: { trigger: "item", ...TOOLTIP_STYLE },
      legend: { bottom: "0%", textStyle: { color: CHART_TEXT_COLOR } },
      series: [
        {
          type: "pie",
          radius: ["50%", "72%"],
          center: ["50%", "45%"],
          label: { show: false },
          data: (["true_positive", "false_positive", "uncertain"] as const).map((k) => ({
            name: VERDICT_LABELS[k],
            value: aiAnalytics?.verdict_counts?.[k] ?? 0,
            itemStyle: { color: VERDICT_COLORS[k] },
          })),
        },
      ],
    }),
    [aiAnalytics],
  );

  const aiSeverityOption = useMemo(
    () => ({
      backgroundColor: "transparent",
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, ...TOOLTIP_STYLE },
      legend: { textStyle: { color: CHART_TEXT_COLOR } },
      grid: { left: "4%", right: "4%", bottom: "12%", top: "40px", containLabel: true },
      xAxis: {
        type: "category",
        data: SEVERITY_KEYS,
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        axisLabel: { color: CHART_TEXT_COLOR },
      },
      yAxis: {
        type: "value",
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        splitLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        axisLabel: { color: CHART_TEXT_COLOR },
      },
      series: (["true_positive", "false_positive", "uncertain"] as const).map((k) => ({
        name: VERDICT_LABELS[k],
        type: "bar",
        stack: "verdict",
        itemStyle: { color: VERDICT_COLORS[k] },
        data: SEVERITY_KEYS.map(
          (severity) => aiAnalytics?.severity_by_verdict?.[severity]?.[k] ?? 0,
        ),
      })),
    }),
    [aiAnalytics],
  );

  const pipelineTrendOption = useMemo(
    () => ({
      backgroundColor: "transparent",
      tooltip: { trigger: "axis", ...TOOLTIP_STYLE },
      legend: { textStyle: { color: CHART_TEXT_COLOR } },
      grid: { left: "3%", right: "3%", bottom: "10%", top: "42px", containLabel: true },
      xAxis: {
        type: "category",
        data: pipelineTrend.map((item) => formatWeekLabel(item.week)),
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        axisLabel: { color: CHART_TEXT_COLOR, rotate: pipelineTrend.length > 8 ? 30 : 0 },
      },
      yAxis: [
        {
          type: "value",
          name: "Runs",
          nameTextStyle: { color: CHART_TEXT_COLOR },
          axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
          splitLine: { lineStyle: { color: CHART_AXIS_COLOR } },
          axisLabel: { color: CHART_TEXT_COLOR },
        },
        {
          type: "value",
          name: "Warnings Rate",
          nameTextStyle: { color: CHART_TEXT_COLOR },
          axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
          splitLine: { show: false },
          axisLabel: { color: CHART_TEXT_COLOR },
        },
      ],
      series: [
        {
          name: "Runs",
          type: "bar",
          yAxisIndex: 0,
          barMaxWidth: 24,
          data: pipelineTrend.map((item) => item.runs),
          itemStyle: { color: "#4dd4ff", borderRadius: [3, 3, 0, 0] },
        },
        {
          name: "Warnings Rate",
          type: "line",
          yAxisIndex: 1,
          smooth: true,
          data: pipelineTrend.map((item) => item.warnings_rate),
          itemStyle: { color: "#fb923c" },
        },
      ],
    }),
    [pipelineTrend],
  );

  // ─── Render ────────────────────────────────────────────────────────────────

  const lastUpdated = dashboard.dataUpdatedAt
    ? new Date(dashboard.dataUpdatedAt).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : null;

  const dashboardError = dashboard.isError ? dashboard.error : null;
  // A filter change keeps the previous charts, dimmed, until the new summary arrives.
  const isRefreshing = dashboard.isPlaceholderData;

  return (
    <div className={filterPanelOpen ? "grid min-h-0 gap-6 lg:grid-cols-[280px_minmax(0,1fr)]" : ""}>
      {filterPanelOpen ? (
        <div className="aist-scrollbar mb-4 self-start overflow-auto lg:sticky lg:top-24 lg:mb-0 lg:max-h-[calc(100vh-140px)]">
          <FilterPanel {...filters.panelProps} />
        </div>
      ) : null}

      <section className="min-w-0 space-y-4">
        <header className="flex flex-col gap-3 rounded-2xl border border-night-500 bg-night-700/90 p-4">
          <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
            <div>
              <h1 className="flex items-center gap-2 text-lg font-semibold text-slate-100">
                Security Dashboard
                {dashboard.isFetching && !dashboard.isLoading ? (
                  <svg
                    className="h-3.5 w-3.5 animate-spin text-brand-500"
                    viewBox="0 0 24 24"
                    fill="none"
                    aria-label="Refreshing"
                  >
                    <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" strokeDasharray="40 20" strokeLinecap="round" />
                  </svg>
                ) : null}
              </h1>
              {lastUpdated ? (
                <p className="text-sm text-slate-400">Last updated: {lastUpdated}</p>
              ) : null}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                className="aist-icon-button h-9 px-3 text-xs font-semibold uppercase tracking-[0.14em]"
                aria-pressed={filterPanelOpen}
                onClick={toggleFilterPanel}
              >
                <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
                  <path fill="currentColor" d="M10 18h4v-2h-4v2Zm-7-10v2h18V8H3Zm3 7h12v-2H6v2Z" />
                </svg>
                Filters
                {hasFilters ? (
                  <span
                    className={`grid h-[18px] min-w-[18px] place-items-center rounded-full border px-1 text-[10px] tracking-normal ${ACCENT_SELECTED_CLASS}`}
                    aria-label={`${filters.chips.length} active`}
                  >
                    {filters.chips.length}
                  </span>
                ) : null}
              </button>
              <Link
                to={buildFindingsLink()}
                className="aist-icon-button h-9 px-3 text-xs font-semibold uppercase tracking-[0.14em]"
              >
                Open in Findings
                <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
                  <path d="M5 12h14M13 6l6 6-6 6" />
                </svg>
              </Link>
            </div>
          </div>
        </header>

        {filterPanelOpen ? null : (
          <ActiveFiltersBar chips={filters.chips} onRemove={filters.removeChip} onClearAll={filters.clearAll} />
        )}

        {dashboardError ? (
          <PageErrorState error={dashboardError} fallbackTitle="Dashboard unavailable" />
        ) : (
          <div
            aria-busy={isRefreshing}
            className={["space-y-4 transition-opacity", isRefreshing ? "opacity-50" : ""].join(" ").trim()}
          >

            <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
              <KpiCard label="Active Findings" value={kpi?.total_active ?? 0} />
              <KpiCard
                label="Critical & High"
                value={kpi?.critical_high ?? 0}
                accent="text-danger-500"
              />
              <KpiCard label="Total Findings" value={kpi?.total_findings ?? 0} />
              <KpiCard
                label="Risk Accepted"
                value={kpi?.risk_accepted ?? 0}
                accent="text-orange-400"
              />
              <KpiCard label="Projects" value={kpi?.projects_count ?? 0} />
            </div>

            {dashboard.data?.triage_progress ? (
              <TriageProgressSection
                triage={dashboard.data.triage_progress}
                findingsLink={buildFindingsLink}
                notApplicable={
                  activeChartsNotApplicable ? <ChartNotApplicable height={200} onResetStatus={resetStatus} /> : undefined
                }
              />
            ) : null}

            {dashboard.isLoading ? (
              <DashboardSkeleton />
            ) : dashboard.data && kpi?.total_findings === 0 && hasFilters ? (
              <div className="flex flex-col items-center gap-4 rounded-2xl border border-night-500 bg-night-700/80 px-6 py-16 text-center">
                <div>
                  <p className="text-sm font-semibold text-slate-300">No findings match the current filters</p>
                  <p className="mt-1 text-xs text-slate-500">Try adjusting or clearing your filters to see more results.</p>
                </div>
                <button
                  type="button"
                  className="aist-icon-button h-9 px-4 text-xs font-semibold uppercase tracking-[0.14em]"
                  onClick={filters.clearAll}
                >
                  Clear filters
                </button>
              </div>
            ) : dashboard.data && kpi?.total_findings === 0 ? (
              <div className="flex flex-col items-center gap-4 rounded-2xl border border-night-500 bg-night-700/80 px-6 py-16 text-center">
                <svg viewBox="0 0 24 24" className="h-12 w-12 text-slate-600" aria-hidden="true">
                  <path fill="currentColor" d="M12 1 3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4Zm0 2.18 7 3.12V11c0 4.52-3.07 8.77-7 9.93-3.93-1.16-7-5.41-7-9.93V6.3l7-3.12Z" />
                </svg>
                <div>
                  <p className="text-sm font-semibold text-slate-300">No findings yet</p>
                  <p className="mt-1 text-xs text-slate-500">
                    {kpi?.projects_count === 0
                      ? "Set up a project and run a scan pipeline to start analysing your code."
                      : "Run a scan pipeline on your projects to start seeing results here."}
                  </p>
                </div>
                {kpi?.projects_count === 0 ? (
                  <Link
                    to={getRoute("ui_products_path")}
                    className="aist-icon-button h-9 px-4 text-xs font-semibold uppercase tracking-[0.14em]"
                  >
                    Set up a project
                  </Link>
                ) : null}
              </div>
            ) : (
              <>
                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                  <ChartCard
                    title="Severity Distribution"
                    subtitle="Active findings by severity level · click a segment to filter"
                  >
                    {activeChartsNotApplicable ? (
                      <ChartNotApplicable onResetStatus={resetStatus} />
                    ) : (
                      <ReactECharts
                        option={severityDonutOption}
                        style={{ width: "100%", height: "280px" }}
                        opts={{ renderer: "svg" }}
                        onEvents={{
                          click: (params: { name?: string; componentType?: string }) => {
                            if (params.componentType === "series" && params.name) {
                              navigate(buildActiveFindingsLink({ severities: [params.name as Severity] }));
                            }
                          },
                        }}
                      />
                    )}
                  </ChartCard>

                  <ChartCard
                    title="Top Vulnerable Projects"
                    subtitle="Ranked by total active finding count · click to open findings"
                  >
                    {activeChartsNotApplicable ? (
                      <ChartNotApplicable onResetStatus={resetStatus} />
                    ) : topProjects.length === 0 ? (
                      <ChartEmpty height={280} />
                    ) : (
                      <ReactECharts
                        option={topProjectsOption}
                        style={{ width: "100%", height: "280px" }}
                        opts={{ renderer: "svg" }}
                        onEvents={{
                          click: (params: { dataIndex?: number }) => {
                            const project = topProjects[params.dataIndex ?? -1];
                            if (project?.project_id) {
                              navigate(buildActiveFindingsLink({ projectId: project.project_id, projectVersion: "" }));
                            }
                          },
                        }}
                      />
                    )}
                  </ChartCard>
                </div>

                <ChartCard
                  title="Finding Status Breakdown"
                  subtitle="Lifecycle status distribution across the selected findings"
                >
                  <ReactECharts
                    option={statusBreakdownOption}
                    style={{ width: "100%", height: "120px" }}
                    opts={{ renderer: "svg" }}
                  />
                </ChartCard>

                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                  <ChartCard
                    title="Findings Aging Heatmap"
                    subtitle="Open findings by age bracket and severity · click to filter"
                  >
                    {activeChartsNotApplicable ? (
                      <ChartNotApplicable height={320} onResetStatus={resetStatus} />
                    ) : agingData.length === 0 ? (
                      <ChartEmpty height={320} />
                    ) : (
                      <ReactECharts
                        option={agingHeatmapOption}
                        style={{ width: "100%", height: "320px" }}
                        opts={{ renderer: "svg" }}
                        onEvents={{
                          click: (params: { value?: [number, number, number] }) => {
                            const value = params.value;
                            if (!value) return;
                            const bucket = agingBuckets[value[0]];
                            const severity = agingSeverities[value[1]];
                            if (!bucket || !severity) return;
                            navigate(
                              buildActiveFindingsLink({
                                severities: [severity as Severity],
                                ...agingBucketRange(bucket, now),
                              }),
                            );
                          },
                        }}
                      />
                    )}
                  </ChartCard>

                  <ChartCard
                    title="Risk Trend"
                    subtitle="Weekly new vs resolved · Net Delta = New − Mitigated · click a point to filter"
                  >
                    {riskTrend.length === 0 ? (
                      <ChartEmpty height={320} />
                    ) : (
                      <ReactECharts
                        option={riskTrendOption}
                        style={{ width: "100%", height: "320px" }}
                        opts={{ renderer: "svg" }}
                        onEvents={{
                          click: (params: { dataIndex?: number; seriesName?: string }) => {
                            const idx = params.dataIndex ?? -1;
                            const row = riskTrend[idx];
                            if (!row) return;
                            const range = weekRange(row.week);
                            navigate(
                              params.seriesName === "Mitigated"
                                ? buildFindingsLink({ ...range, status: "Non-Active" })
                                : buildActiveFindingsLink(range),
                            );
                          },
                        }}
                      />
                    )}
                  </ChartCard>
                </div>

                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                  <ChartCard
                    title="Top CWE Exposure"
                    subtitle="Active findings by vulnerability class (CWE) · click to filter"
                  >
                    {activeChartsNotApplicable ? (
                      <ChartNotApplicable height={320} onResetStatus={resetStatus} />
                    ) : cweDistribution.length === 0 ? (
                      <ChartEmpty height={320} />
                    ) : (
                      <ReactECharts
                        option={cweBarOption}
                        style={{ width: "100%", height: "320px" }}
                        opts={{ renderer: "svg" }}
                        onEvents={{
                          click: (params: { data?: { cwe?: number } }) => {
                            const cwe = params.data?.cwe;
                            if (!cwe) return;
                            navigate(buildActiveFindingsLink({ cwe: String(cwe) }));
                          },
                        }}
                      />
                    )}
                  </ChartCard>

                  <ChartCard
                    title="AI Verdict Analytics"
                    subtitle="AI-assisted triage: verdict distribution and severity breakdown · click to filter"
                  >
                    <div className="grid grid-cols-1 gap-3">
                      <ReactECharts
                        option={aiVerdictOption}
                        style={{ width: "100%", height: "200px" }}
                        opts={{ renderer: "svg" }}
                        onEvents={{
                          click: (params: { name?: string }) => {
                            const map: Record<string, string> = {
                              [VERDICT_LABELS.true_positive]: "ai_tp",
                              [VERDICT_LABELS.false_positive]: "ai_fp",
                              [VERDICT_LABELS.uncertain]: "ai_u",
                            };
                            const aiStatus = params.name ? map[params.name] : "";
                            if (!aiStatus) return;
                            navigate(buildFindingsLink({ aiStatus }));
                          },
                        }}
                      />
                      <ReactECharts
                        option={aiSeverityOption}
                        style={{ width: "100%", height: "180px" }}
                        opts={{ renderer: "svg" }}
                        onEvents={{
                          click: (params: { name?: string; seriesName?: string }) => {
                            const map: Record<string, string> = {
                              [VERDICT_LABELS.true_positive]: "ai_tp",
                              [VERDICT_LABELS.false_positive]: "ai_fp",
                              [VERDICT_LABELS.uncertain]: "ai_u",
                            };
                            const aiStatus = params.seriesName ? map[params.seriesName] : "";
                            const severity = params.name;
                            if (!aiStatus || !severity) return;
                            navigate(buildFindingsLink({ aiStatus, severities: [severity as Severity] }));
                          },
                        }}
                      />
                    </div>
                  </ChartCard>
                </div>

                {pipelineTrend.length > 0 && (
                  <ChartCard
                    title="Pipeline Performance"
                    subtitle="Weekly scan run count and warning rate"
                    badge={
                      <ScopeBadge
                        label="Project filter only"
                        hint="Pipelines are scans, not findings: only the Project filter applies here."
                      />
                    }
                  >
                    <ReactECharts
                      option={pipelineTrendOption}
                      style={{ width: "100%", height: "280px" }}
                      opts={{ renderer: "svg" }}
                    />
                  </ChartCard>
                )}
              </>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
