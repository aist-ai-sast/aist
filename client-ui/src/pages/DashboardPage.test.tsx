// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { FindingsFilterUrlState } from "../lib/findingsFilterUrl";
import type { DashboardSummary } from "../lib/queries";

function summary(overrides: Partial<DashboardSummary["kpi"]> = {}): DashboardSummary {
  return {
    kpi: { total_active: 3, critical_high: 2, total_findings: 5, risk_accepted: 1, projects_count: 1, ...overrides },
    severity_distribution: { Critical: 1, High: 1, Medium: 1, Low: 0, Info: 0 },
    top_projects: [
      { project_id: 5, name: "dev/cloud_portal", critical: 1, high: 1, medium: 1, low: 0, info: 0, total_active: 3 },
    ],
    finding_status_breakdown: {
      active: 3, mitigated: 1, risk_accepted: 1, under_review: 0, false_positive: 0, out_of_scope: 0,
    },
    findings_aging_heatmap: {
      buckets: ["0_7", "8_30", "31_90", "90_plus"],
      severities: ["Critical", "High"],
      matrix: { Critical: { "0_7": 1 }, High: { "0_7": 1 } },
    },
    risk_trend: [{ week: "2026-09-21", new_findings: 2, mitigated_findings: 1, net: 1 }],
    pipeline_performance_trend: [{ week: "2026-09-21", runs: 2, median_duration_seconds: 60, warnings_rate: 0 }],
    cwe_distribution: [{ cwe: 89, count: 1, title: "SQL Injection", description: "", impact: "", url: "" }],
    ai_verdict_analytics: {
      total: 1,
      verdict_counts: { true_positive: 1, false_positive: 0, uncertain: 0 },
      severity_by_verdict: {
        Critical: { true_positive: 1, false_positive: 0, uncertain: 0 },
        High: { true_positive: 0, false_positive: 0, uncertain: 0 },
        Medium: { true_positive: 0, false_positive: 0, uncertain: 0 },
        Low: { true_positive: 0, false_positive: 0, uncertain: 0 },
        Info: { true_positive: 0, false_positive: 0, uncertain: 0 },
      },
      uncertainty_buckets: { low: 1, medium: 0, high: 0 },
    },
    work_item_coverage: { total_linked: 1, coverage_pct: 33.3, by_status: { OPEN: 1 } },
  };
}

let mockSummary: { data?: DashboardSummary; isLoading: boolean; isFetching: boolean; isError: boolean; isPlaceholderData: boolean; error: unknown; dataUpdatedAt: number };
const dashboardCalls: FindingsFilterUrlState[] = [];

vi.mock("echarts-for-react", () => ({
  default: () => <div data-testid="chart" />,
}));

vi.mock("../components/FilterPanel", () => ({
  default: ({ selectedSeverities }: { selectedSeverities: string[] }) => (
    <aside data-testid="filter-panel">{selectedSeverities.join(",")}</aside>
  ),
}));

vi.mock("../lib/queries", () => ({
  useProjects: () => ({ data: [{ id: 5, name: "dev/cloud_portal" }] }),
  useFindingTagsByProject: () => ({ data: { names: ["auth", "test"], counts: {} }, isSuccess: true }),
  useDashboardSummary: (filters: FindingsFilterUrlState) => {
    dashboardCalls.push(filters);
    return mockSummary;
  },
}));

vi.mock("../lib/routes", () => ({
  getRoute: (name: string) => (name === "ui_findings_path" ? "/findings" : "/projects"),
}));

import DashboardPage from "./DashboardPage";

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.search}</div>;
}

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <DashboardPage />
      <LocationProbe />
    </MemoryRouter>,
  );
}

function currentSearch() {
  return new URLSearchParams(screen.getByTestId("location").textContent ?? "");
}

describe("DashboardPage filter", () => {
  beforeEach(() => {
    dashboardCalls.length = 0;
    mockSummary = {
      data: summary(),
      isLoading: false,
      isFetching: false,
      isError: false,
      isPlaceholderData: false,
      error: null,
      dataUpdatedAt: Date.now(),
    };
    window.localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("opens without filters: panel collapsed, no chips, no project select in the header", () => {
    renderAt("/dashboard");

    expect(screen.queryByTestId("filter-panel")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Filters/ })).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByLabelText("Active filters")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Project" })).not.toBeInTheDocument();
  });

  it("opens the panel for a shared link that carries filters and sends them to the summary", () => {
    renderAt("/dashboard?severity=High&tags=auth");

    expect(screen.getByTestId("filter-panel")).toHaveTextContent("High");
    expect(screen.getByRole("button", { name: /Filters/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("2 active")).toHaveTextContent("2");
    expect(screen.queryByLabelText("Active filters")).not.toBeInTheDocument();
    const lastCall = dashboardCalls[dashboardCalls.length - 1];
    expect(lastCall?.severities).toEqual(["High"]);
    expect(lastCall?.tags.include).toEqual(["auth"]);
  });

  it("shows the chips once the panel is collapsed, and a chip removes its filter from the URL", () => {
    renderAt("/dashboard?severity=High&tags=auth");

    fireEvent.click(screen.getByRole("button", { name: /Filters/ }));

    expect(screen.queryByTestId("filter-panel")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Active filters")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove filter Severity High" }));
    expect(currentSearch().get("severity")).toBeNull();
    expect(currentSearch().get("tags")).toBe("auth");
  });

  it("remembers the panel choice for the next visit without filters", () => {
    renderAt("/dashboard");
    fireEvent.click(screen.getByRole("button", { name: /Filters/ }));
    cleanup();

    renderAt("/dashboard");

    expect(screen.getByTestId("filter-panel")).toBeInTheDocument();
  });

  it("renders when browser storage is blocked", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });

    renderAt("/dashboard");
    fireEvent.click(screen.getByRole("button", { name: /Filters/ }));

    expect(screen.getByTestId("filter-panel")).toBeInTheDocument();
  });

  it("opens the same selection in Findings", () => {
    renderAt("/dashboard?project_id=5&severity=High&tags=auth");

    expect(screen.getByRole("link", { name: /Open in Findings/ })).toHaveAttribute(
      "href",
      "/findings?project_id=5&severity=High&tags=auth",
    );
  });

  it("marks active-only charts as not applicable for non-active findings and resets the status", () => {
    renderAt("/dashboard?active=false");

    // Work Item Coverage, Severity Distribution, Top Projects, Aging Heatmap, Top CWE.
    expect(screen.getAllByText("Not applicable to the current filters")).toHaveLength(5);
    fireEvent.click(screen.getAllByRole("button", { name: "Reset Status" })[0]);
    expect(currentSearch().get("active")).toBeNull();
    expect(screen.queryByText("Not applicable to the current filters")).not.toBeInTheDocument();
  });

  it("labels pipeline performance as outside the finding filter", () => {
    renderAt("/dashboard?severity=High");

    expect(screen.getByText("Project filter only")).toBeInTheDocument();
  });

  it("dims the previous charts while a new filter loads instead of showing a skeleton", () => {
    mockSummary = { ...mockSummary, isFetching: true, isPlaceholderData: true };
    renderAt("/dashboard?severity=High");

    expect(screen.getByLabelText("Refreshing")).toBeInTheDocument();
    expect(document.querySelector("[aria-busy='true']")).not.toBeNull();
    expect(screen.getAllByTestId("chart").length).toBeGreaterThan(0);
  });

  it("tells a filtered empty selection apart from a workspace without findings", () => {
    mockSummary = { ...mockSummary, data: summary({ total_findings: 0, total_active: 0 }) };
    renderAt("/dashboard?severity=Info");

    expect(screen.getByText("No findings match the current filters")).toBeInTheDocument();
    expect(screen.queryByText("No findings yet")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(currentSearch().toString()).toBe("");
  });

  it("keeps the panel when the filter is rejected by the server", () => {
    mockSummary = { ...mockSummary, data: undefined, isError: true, error: new Error("Bad filter") };
    renderAt("/dashboard?created_gte=bad");

    expect(screen.getByTestId("filter-panel")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Filters/ })).toBeInTheDocument();
  });
});
