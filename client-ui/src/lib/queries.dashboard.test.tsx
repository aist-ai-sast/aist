// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_FINDINGS_FILTERS, type FindingsFilterUrlState, toFindingsApiFilters } from "./findingsFilterUrl";

const fetchJson = vi.fn();

vi.mock("./api", () => ({
  fetchJson: (url: string) => fetchJson(url),
  normalizeList: (payload: { results?: unknown[] }) => payload.results ?? [],
}));

vi.mock("./routes", () => ({
  getRoute: (name: string) => (name === "dashboard_summary_url" ? "/summary/dashboard/" : "/api/findings/"),
}));

import { buildFindingsParams, buildFindingsQuery, useDashboardSummary } from "./queries";

const FULL_STATE: FindingsFilterUrlState = {
  projectId: 5,
  pipelineId: "pipe-1",
  title: "SQL",
  createdFrom: "2026-09-01",
  createdTo: "2026-09-20",
  statusUpdatedFrom: "2026-09-02",
  statusUpdatedTo: "2026-09-03",
  mitigatedFrom: "2026-09-04",
  mitigatedTo: "2026-09-05",
  projectVersion: "main",
  file: "src/app.py",
  cwe: "79,89",
  severities: ["Critical", "High"],
  tags: { include: ["auth"], exclude: ["test"], matchMode: "all" },
  status: "Active",
  risk: ["risk_accepted"],
  aiStatus: "ai_tp",
  workItemStatus: "OPEN",
};

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

describe("buildFindingsQuery", () => {
  it("is the exact query the Findings list sends for the same state", () => {
    expect(buildFindingsQuery(FULL_STATE).toString()).toBe(
      buildFindingsParams(toFindingsApiFilters(FULL_STATE)).toString(),
    );
    expect(buildFindingsQuery(FULL_STATE).get("tags__and")).toBe("auth");
    expect(buildFindingsQuery(FULL_STATE).get("not_tags")).toBe("test");
    expect(buildFindingsQuery(FULL_STATE).get("active")).toBe("true");
  });

  it("is empty for the default filter", () => {
    expect(buildFindingsQuery(DEFAULT_FINDINGS_FILTERS).toString()).toBe("");
  });
});

describe("useDashboardSummary", () => {
  beforeEach(() => {
    fetchJson.mockReset();
  });

  it("sends the filter to the summary endpoint", async () => {
    fetchJson.mockResolvedValue({ kpi: { total_findings: 3 } });
    const { result } = renderHook(() => useDashboardSummary({ ...DEFAULT_FINDINGS_FILTERS, severities: ["High"] }), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchJson).toHaveBeenCalledWith("/summary/dashboard/?severity=High");
  });

  it("calls the bare endpoint without a filter", async () => {
    fetchJson.mockResolvedValue({ kpi: { total_findings: 9 } });
    const { result } = renderHook(() => useDashboardSummary(DEFAULT_FINDINGS_FILTERS), { wrapper: wrapper() });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchJson).toHaveBeenCalledWith("/summary/dashboard/");
  });

  it("keeps the previous summary on screen while a new filter loads", async () => {
    let resolveSecond: (value: unknown) => void = () => undefined;
    fetchJson
      .mockResolvedValueOnce({ kpi: { total_findings: 9 } })
      .mockImplementationOnce(() => new Promise((resolve) => { resolveSecond = resolve; }));
    const { result, rerender } = renderHook(({ state }) => useDashboardSummary(state), {
      wrapper: wrapper(),
      initialProps: { state: DEFAULT_FINDINGS_FILTERS },
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    rerender({ state: { ...DEFAULT_FINDINGS_FILTERS, severities: ["Critical"] } });

    await waitFor(() => expect(result.current.isFetching).toBe(true));
    expect(result.current.isPlaceholderData).toBe(true);
    expect(result.current.data?.kpi.total_findings).toBe(9);

    resolveSecond({ kpi: { total_findings: 1 } });
    await waitFor(() => expect(result.current.data?.kpi.total_findings).toBe(1));
    expect(result.current.isPlaceholderData).toBe(false);
  });
});
