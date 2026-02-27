import ReactECharts from "echarts-for-react";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import PageErrorState from "../components/PageErrorState";
import SelectField from "../components/SelectField";
import type { DashboardSummary } from "../lib/queries";
import { useDashboardSummary, useProjects } from "../lib/queries";
import { getRoute } from "../lib/routes";

const CHART_TEXT_COLOR = "#94a3b8";
const CHART_AXIS_COLOR = "rgba(45, 67, 105, 0.85)";
const SEVERITY_COLORS: Record<string, string> = {
  Critical: "#ff6b6b",
  High: "#fb923c",
  Medium: "#fbbf24",
  Low: "#94a3b8",
  Info: "#64748b",
};
const STATUS_COLORS: Record<string, string> = {
  active: "#4dd4ff",
  mitigated: "#34d399",
  risk_accepted: "#fb923c",
  under_review: "#a78bfa",
  false_positive: "#64748b",
  out_of_scope: "#475569",
};
const SEVERITY_KEYS = ["Critical", "High", "Medium", "Low", "Info"] as const;
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
  "0_7": "0-7d",
  "8_30": "8-30d",
  "31_90": "31-90d",
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

type TopProject = DashboardSummary["top_projects"][number];

function getProjectSeverity(project: TopProject, key: (typeof SEVERITY_KEYS)[number]): number {
  if (key === "Critical") return project.critical;
  if (key === "High") return project.high;
  if (key === "Medium") return project.medium;
  if (key === "Low") return project.low;
  return project.info;
}

function KpiCard({
  label,
  value,
  accent,
}: {
  label: string;
  value: number;
  accent?: string;
}) {
  return (
    <div className="rounded-2xl border border-night-500 bg-night-700/90 p-4">
      <div className="text-xs uppercase tracking-[0.16em] text-slate-400">{label}</div>
      <div className={["mt-2 text-3xl font-bold", accent ?? "text-slate-100"].join(" ")}>
        {value.toLocaleString()}
      </div>
    </div>
  );
}

const TOOLTIP_STYLE = {
  backgroundColor: "rgba(15,23,42,0.95)",
  borderColor: CHART_AXIS_COLOR,
  textStyle: { color: "#e2e8f0" },
};

export default function DashboardPage() {
  const navigate = useNavigate();
  const projects = useProjects();
  const [selectedProjectId, setSelectedProjectId] = useState<number | undefined>(undefined);
  const dashboard = useDashboardSummary(selectedProjectId);

  const projectOptions = useMemo(
    () => [
      { value: "all", label: "All projects" },
      ...((projects.data ?? []).map((p) => ({ value: String(p.id), label: p.name }))),
    ],
    [projects.data],
  );
  const now = new Date();
  const toDateParam = (value: Date) => value.toISOString().slice(0, 10);
  const addDays = (value: Date, days: number) => {
    const next = new Date(value);
    next.setDate(next.getDate() + days);
    return next;
  };
  const buildFindingsLink = (extra: Record<string, string | undefined>) => {
    const params = new URLSearchParams();
    if (selectedProjectId) params.set("project", String(selectedProjectId));
    Object.entries(extra).forEach(([key, value]) => {
      if (value) params.set(key, value);
    });
    return `${getRoute("ui_findings_path")}?${params.toString()}`;
  };
  const buildPipelinesLink = (extra: Record<string, string | undefined>) => {
    const params = new URLSearchParams();
    if (selectedProjectId) params.set("project", String(selectedProjectId));
    Object.entries(extra).forEach(([key, value]) => {
      if (value) params.set(key, value);
    });
    return `${getRoute("ui_pipelines_path")}?${params.toString()}`;
  };

  if (dashboard.isError) {
    return <PageErrorState error={dashboard.error} fallbackTitle="Dashboard unavailable" />;
  }

  const kpi = dashboard.data?.kpi;
  const severityDist = dashboard.data?.severity_distribution ?? {};
  const topProjects = dashboard.data?.top_projects ?? [];
  const statusBreakdown = dashboard.data?.finding_status_breakdown ?? {};
  const agingHeatmap = dashboard.data?.findings_aging_heatmap;
  const riskTrend = dashboard.data?.risk_trend ?? [];
  const pipelinePerfTrend = dashboard.data?.pipeline_performance_trend ?? [];
  const aiAnalytics = dashboard.data?.ai_verdict_analytics;

  const severityDonutOption = {
    backgroundColor: "transparent",
    tooltip: {
      trigger: "item",
      formatter: "{b}: {c} ({d}%)",
      ...TOOLTIP_STYLE,
    },
    legend: {
      bottom: "0%",
      textStyle: { color: CHART_TEXT_COLOR },
      itemWidth: 10,
      itemHeight: 10,
    },
    series: [
      {
        type: "pie",
        radius: ["52%", "72%"],
        center: ["50%", "48%"],
        data: SEVERITY_KEYS.map((key) => ({
          name: key,
          value: severityDist[key] ?? 0,
          itemStyle: { color: SEVERITY_COLORS[key] },
        })),
        emphasis: { scale: true },
        label: { show: false },
        cursor: "pointer",
      },
    ],
  };

  const topProjectNames = topProjects.map((p) => p.name);
  const topProjectsOption = {
    backgroundColor: "transparent",
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      ...TOOLTIP_STYLE,
    },
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
      data: topProjectNames,
      axisLabel: { color: CHART_TEXT_COLOR, width: 120, overflow: "truncate" },
      axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
    },
    ...(topProjects.length > 5
      ? { dataZoom: [{ type: "slider", yAxisIndex: 0, show: true, startValue: 0, endValue: 4 }] }
      : {}),
    series: SEVERITY_KEYS.map((key) => ({
      name: key,
      type: "bar",
      stack: "total",
      itemStyle: { color: SEVERITY_COLORS[key] },
      data: topProjects.map((p) => getProjectSeverity(p, key)),
    })),
  };

  const statusBreakdownOption = {
    backgroundColor: "transparent",
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      ...TOOLTIP_STYLE,
    },
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
    series: STATUS_KEYS.map((key) => ({
      name: STATUS_LABELS[key],
      type: "bar",
      stack: "total",
      barMaxWidth: 32,
      itemStyle: { color: STATUS_COLORS[key] },
      data: [statusBreakdown[key] ?? 0],
    })),
  };
  const agingBuckets = agingHeatmap?.buckets ?? [];
  const agingSeverities = agingHeatmap?.severities ?? [];
  const agingData = agingSeverities.flatMap((severity, severityIdx) =>
    agingBuckets.map((bucket, bucketIdx) => [
      bucketIdx,
      severityIdx,
      agingHeatmap?.matrix?.[severity]?.[bucket] ?? 0,
    ]),
  );
  const agingHeatmapOption = {
    backgroundColor: "transparent",
    tooltip: {
      position: "top",
      formatter: (params: { value?: [number, number, number] }) => {
        const value = params.value ?? [0, 0, 0];
        const bucket = agingBuckets[value[0]] ?? "";
        const severity = agingSeverities[value[1]] ?? "";
        return `${severity} · ${AGE_BUCKET_LABELS[bucket] ?? bucket}: ${value[2]}`;
      },
      ...TOOLTIP_STYLE,
    },
    grid: { left: "4%", right: "4%", bottom: "14%", top: "8%", containLabel: true },
    xAxis: {
      type: "category",
      data: agingBuckets.map((bucket) => AGE_BUCKET_LABELS[bucket] ?? bucket),
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
  };

  const riskTrendOption = {
    backgroundColor: "transparent",
    tooltip: { trigger: "axis", ...TOOLTIP_STYLE },
    legend: { textStyle: { color: CHART_TEXT_COLOR } },
    grid: { left: "3%", right: "3%", bottom: "10%", top: "42px", containLabel: true },
    xAxis: {
      type: "category",
      data: riskTrend.map((item) => item.week.slice(5)),
      axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
      axisLabel: { color: CHART_TEXT_COLOR },
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
        name: "Net",
        type: "bar",
        data: riskTrend.map((item) => item.net),
        itemStyle: { color: "#fbbf24" },
      },
    ],
  };

  const pipelinePerfOption = {
    backgroundColor: "transparent",
    tooltip: { trigger: "axis", ...TOOLTIP_STYLE },
    legend: { textStyle: { color: CHART_TEXT_COLOR } },
    grid: { left: "3%", right: "3%", bottom: "10%", top: "42px", containLabel: true },
    xAxis: {
      type: "category",
      data: pipelinePerfTrend.map((item) => item.week.slice(5)),
      axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
      axisLabel: { color: CHART_TEXT_COLOR },
    },
    yAxis: [
      {
        type: "value",
        name: "Runs",
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        splitLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        axisLabel: { color: CHART_TEXT_COLOR },
      },
      {
        type: "value",
        name: "Minutes / %",
        axisLine: { lineStyle: { color: CHART_AXIS_COLOR } },
        splitLine: { show: false },
        axisLabel: { color: CHART_TEXT_COLOR },
      },
    ],
    series: [
      {
        name: "Runs",
        type: "bar",
        data: pipelinePerfTrend.map((item) => item.runs),
        itemStyle: { color: "#4dd4ff" },
      },
      {
        name: "Median Duration (min)",
        type: "line",
        yAxisIndex: 1,
        smooth: true,
        data: pipelinePerfTrend.map((item) => Math.round(item.median_duration_seconds / 60)),
        itemStyle: { color: "#fbbf24" },
      },
      {
        name: "Warnings Rate (%)",
        type: "line",
        yAxisIndex: 1,
        smooth: true,
        data: pipelinePerfTrend.map((item) => Math.round(item.warnings_rate * 100)),
        itemStyle: { color: "#f87171" },
      },
    ],
  };

  const aiVerdictOption = {
    backgroundColor: "transparent",
    tooltip: { trigger: "item", ...TOOLTIP_STYLE },
    legend: { bottom: "0%", textStyle: { color: CHART_TEXT_COLOR } },
    series: [
      {
        type: "pie",
        radius: ["50%", "72%"],
        center: ["50%", "45%"],
        label: { show: false },
        data: (["true_positive", "false_positive", "uncertain"] as const).map((key) => ({
          name: VERDICT_LABELS[key],
          value: aiAnalytics?.verdict_counts?.[key] ?? 0,
          itemStyle: { color: VERDICT_COLORS[key] },
        })),
      },
    ],
  };

  const aiSeverityOption = {
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
    series: (["true_positive", "false_positive", "uncertain"] as const).map((key) => ({
      name: VERDICT_LABELS[key],
      type: "bar",
      stack: "verdict",
      itemStyle: { color: VERDICT_COLORS[key] },
      data: SEVERITY_KEYS.map((severity) => aiAnalytics?.severity_by_verdict?.[severity]?.[key] ?? 0),
    })),
  };

  const lastUpdated = dashboard.dataUpdatedAt
    ? new Date(dashboard.dataUpdatedAt).toLocaleTimeString()
    : null;

  return (
    <section className="space-y-4">
      <header className="flex flex-col gap-3 rounded-2xl border border-night-500 bg-night-700/90 p-4">
        <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <h1 className="text-lg font-semibold text-slate-100">Security Dashboard</h1>
            {lastUpdated ? (
              <p className="text-sm text-slate-400">Last updated: {lastUpdated}</p>
            ) : null}
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-slate-400 whitespace-nowrap">Project</span>
            <div className="w-[220px]">
              <SelectField
                label="Project"
                hideLabel
                showIndicator={false}
                value={selectedProjectId ? String(selectedProjectId) : "all"}
                onChange={(value) =>
                  setSelectedProjectId(value && value !== "all" ? Number(value) : undefined)
                }
                options={projectOptions}
                placeholder="All projects"
              />
            </div>
          </div>
        </div>
      </header>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <KpiCard label="Active Findings" value={kpi?.total_active ?? 0} />
        <KpiCard
          label="Critical & High"
          value={kpi?.critical_high ?? 0}
          accent="text-danger-500"
        />
        <KpiCard label="Total Findings" value={kpi?.total_findings ?? 0} />
        <KpiCard label="Projects" value={kpi?.projects_count ?? 0} />
      </div>

      {dashboard.isLoading ? (
        <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-400">
          Loading dashboard...
        </div>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div className="rounded-2xl border border-night-500 bg-night-700/80 p-4">
              <div className="mb-2 text-sm font-semibold text-slate-200">
                Severity Distribution
              </div>
              <ReactECharts
                option={severityDonutOption}
                style={{ width: "100%", height: "280px" }}
                opts={{ renderer: "svg" }}
                onEvents={{
                  click: (params: { name?: string; componentType?: string }) => {
                    if (params.componentType === "series" && params.name) {
                      const urlParams = new URLSearchParams();
                      urlParams.set("severity", params.name);
                      if (selectedProjectId) urlParams.set("project", String(selectedProjectId));
                      navigate(`${getRoute("ui_findings_path")}?${urlParams.toString()}`);
                    }
                  },
                }}
              />
            </div>
            <div className="rounded-2xl border border-night-500 bg-night-700/80 p-4">
              <div className="mb-2 text-sm font-semibold text-slate-200">
                Top Vulnerable Projects
              </div>
              {topProjects.length === 0 ? (
                <div className="flex h-[280px] items-center justify-center text-sm text-slate-400">
                  No active findings
                </div>
              ) : (
                <ReactECharts
                  option={topProjectsOption}
                  style={{ width: "100%", height: "280px" }}
                  opts={{ renderer: "svg" }}
                  onEvents={{
                    click: (params: { dataIndex?: number }) => {
                      const project = topProjects[params.dataIndex ?? -1];
                      if (project?.project_id) {
                        navigate(
                          `${getRoute("ui_findings_path")}?project=${project.project_id}`,
                        );
                      }
                    },
                  }}
                />
              )}
            </div>
          </div>

          <div className="rounded-2xl border border-night-500 bg-night-700/80 p-4">
            <div className="mb-2 text-sm font-semibold text-slate-200">
              Finding Status Breakdown
            </div>
            <ReactECharts
              option={statusBreakdownOption}
              style={{ width: "100%", height: "120px" }}
              opts={{ renderer: "svg" }}
            />
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div className="rounded-2xl border border-night-500 bg-night-700/80 p-4">
              <div className="mb-2 text-sm font-semibold text-slate-200">
                Findings Aging Heatmap
              </div>
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
                    const createdTo = toDateParam(now);
                    let createdFrom = "";
                    if (bucket === "0_7") createdFrom = toDateParam(addDays(now, -7));
                    if (bucket === "8_30") createdFrom = toDateParam(addDays(now, -30));
                    if (bucket === "31_90") createdFrom = toDateParam(addDays(now, -90));
                    const link = buildFindingsLink({
                      severity,
                      active: "true",
                      created_from: createdFrom || undefined,
                      created_to: createdTo,
                    });
                    navigate(link);
                  },
                }}
              />
            </div>
            <div className="rounded-2xl border border-night-500 bg-night-700/80 p-4">
              <div className="mb-2 text-sm font-semibold text-slate-200">
                Risk Trend (New vs Mitigated)
              </div>
              <ReactECharts
                option={riskTrendOption}
                style={{ width: "100%", height: "320px" }}
                opts={{ renderer: "svg" }}
                onEvents={{
                  click: (params: { dataIndex?: number; seriesName?: string }) => {
                    const idx = params.dataIndex ?? -1;
                    const row = riskTrend[idx];
                    if (!row) return;
                    const weekStart = row.week;
                    const weekEnd = toDateParam(addDays(new Date(`${row.week}T00:00:00`), 6));
                    const extra: Record<string, string | undefined> = {
                      created_from: weekStart,
                      created_to: weekEnd,
                    };
                    if (params.seriesName === "Mitigated") {
                      extra.active = "false";
                    }
                    navigate(buildFindingsLink(extra));
                  },
                }}
              />
            </div>
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div className="rounded-2xl border border-night-500 bg-night-700/80 p-4">
              <div className="mb-2 text-sm font-semibold text-slate-200">
                Pipeline Performance Trend
              </div>
              <ReactECharts
                option={pipelinePerfOption}
                style={{ width: "100%", height: "320px" }}
                opts={{ renderer: "svg" }}
                onEvents={{
                  click: (params: { dataIndex?: number }) => {
                    const idx = params.dataIndex ?? -1;
                    const row = pipelinePerfTrend[idx];
                    if (!row) return;
                    const createdFrom = row.week;
                    const createdTo = toDateParam(addDays(new Date(`${row.week}T00:00:00`), 6));
                    navigate(buildPipelinesLink({ created_from: createdFrom, created_to: createdTo }));
                  },
                }}
              />
            </div>
            <div className="rounded-2xl border border-night-500 bg-night-700/80 p-4">
              <div className="mb-2 text-sm font-semibold text-slate-200">
                AI Verdict Analytics
              </div>
              <div className="grid grid-cols-1 gap-3">
                <ReactECharts
                  option={aiVerdictOption}
                  style={{ width: "100%", height: "200px" }}
                  opts={{ renderer: "svg" }}
                  onEvents={{
                    click: (params: { name?: string }) => {
                      const aiStatusByLabel: Record<string, string> = {
                        [VERDICT_LABELS.true_positive]: "ai_tp",
                        [VERDICT_LABELS.false_positive]: "ai_fp",
                        [VERDICT_LABELS.uncertain]: "ai_u",
                      };
                      const aiStatus = params.name ? aiStatusByLabel[params.name] : "";
                      if (!aiStatus) return;
                      navigate(buildFindingsLink({ ai_status: aiStatus }));
                    },
                  }}
                />
                <ReactECharts
                  option={aiSeverityOption}
                  style={{ width: "100%", height: "180px" }}
                  opts={{ renderer: "svg" }}
                  onEvents={{
                    click: (params: { name?: string; seriesName?: string }) => {
                      const verdictByLabel: Record<string, string> = {
                        [VERDICT_LABELS.true_positive]: "ai_tp",
                        [VERDICT_LABELS.false_positive]: "ai_fp",
                        [VERDICT_LABELS.uncertain]: "ai_u",
                      };
                      const aiStatus = params.seriesName ? verdictByLabel[params.seriesName] : "";
                      const severity = params.name;
                      if (!aiStatus || !severity) return;
                      navigate(buildFindingsLink({ ai_status: aiStatus, severity }));
                    },
                  }}
                />
              </div>
            </div>
          </div>
        </>
      )}
    </section>
  );
}
