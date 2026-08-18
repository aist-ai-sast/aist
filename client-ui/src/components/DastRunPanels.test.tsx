// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import DastRunPanels, { DastRunChips } from "./DastRunPanels";
import type { DastRunDetail, DastRunSummary } from "../types";

let detail: DastRunDetail | null = null;
let loading = false;

vi.mock("../lib/queries", () => ({
  usePipelineDastRun: () => ({ data: detail, isLoading: loading }),
}));

// Excerpted from the real report of run 80c744a2be37d91c07a7a8ef97c520be.
const FULL: DastRunDetail = {
  runId: "80c744a2be37d91c07a7a8ef97c520be",
  runType: "deep",
  coverageUnit: "endpoint",
  discovered: 784,
  reachable: 176,
  analysed: 38,
  planned: 10,
  beyondPlan: 28,
  totalTokens: 93_556_484,
  modelCalls: 1_117,
  targetId: "perimeter",
  standId: "external-10host",
  productFamily: "perimeter",
  tier: "external",
  targetHost: "analytics3.test.hdw.mx",
  scanStarted: "2026-08-17T17:37:46Z",
  scanFinished: "2026-08-17T19:56:35Z",
  durationSeconds: 8_329,
  analysedNames: ["analytics3-test-hdw-mx", "auth-test-hdw-mx", "cloud-prod-hdw-mx", "nxlicensed-hdw-mx"],
  beyondPlanNames: ["cloud-prod-hdw-mx", "nxlicensed-hdw-mx"],
  tokens: {
    input_tokens: 2_234,
    output_tokens: 951_808,
    thinking_tokens: 331_554,
    cache_creation_tokens: 2_578_204,
    cache_read_tokens: 90_024_238,
  },
  tokenByPhase: [
    { key: "6", name: "depth: floor, explore, discovery", calls: 660, total_tokens: 56_824_360 },
    { key: "4", calls: 14, total_tokens: 2_233_936 },
  ],
  tokenByAgentType: [{ key: "dast-check-runner", agents: 14, calls: 725, total_tokens: 48_659_086 }],
  agents: 22,
  tokenAccountingConsistent: true,
};

function summary(overrides: Partial<DastRunSummary> = {}): DastRunSummary {
  return {
    runId: FULL.runId,
    runType: FULL.runType,
    coverageUnit: FULL.coverageUnit,
    discovered: FULL.discovered,
    reachable: FULL.reachable,
    analysed: FULL.analysed,
    planned: FULL.planned,
    beyondPlan: FULL.beyondPlan,
    totalTokens: FULL.totalTokens,
    modelCalls: FULL.modelCalls,
    ...overrides,
  };
}

beforeEach(() => {
  detail = FULL;
  loading = false;
});
afterEach(cleanup);

describe("collapsed-row chips", () => {
  it("shows coverage, spend and the beyond-plan warning", () => {
    render(<DastRunChips run={summary()} />);
    expect(screen.getByText("DAST · deep")).toBeInTheDocument();
    expect(screen.getByText("38 / 176 endpoints analysed")).toBeInTheDocument();
    expect(screen.getByText("93.6M tokens")).toBeInTheDocument();
    expect(screen.getByText("28 beyond plan")).toBeInTheDocument();
  });

  it("drops the chips whose numbers the report did not carry", () => {
    render(
      <DastRunChips
        run={summary({ runType: null, analysed: null, reachable: null, totalTokens: null, beyondPlan: null })}
      />,
    );
    expect(screen.getByText("DAST")).toBeInTheDocument();
    expect(screen.queryByText(/analysed/)).not.toBeInTheDocument();
    expect(screen.queryByText(/tokens/)).not.toBeInTheDocument();
    expect(screen.queryByText(/beyond plan/)).not.toBeInTheDocument();
  });

  it("hides the beyond-plan chip when the run stayed inside its plan", () => {
    render(<DastRunChips run={summary({ beyondPlan: 0 })} />);
    expect(screen.queryByText(/beyond plan/)).not.toBeInTheDocument();
  });
});

describe("expanded panels", () => {
  it("renders the run, its coverage funnel and its spend", () => {
    render(<DastRunPanels pipelineId="p1" />);
    expect(screen.getByText("external-10host")).toBeInTheDocument();
    expect(screen.getByText("analytics3.test.hdw.mx")).toBeInTheDocument();
    expect(screen.getByText("2h 18m 49s")).toBeInTheDocument();
    expect(screen.getByText("unit: endpoint")).toBeInTheDocument();
    expect(screen.getByText("Discovered")).toBeInTheDocument();
    expect(screen.getByText("Planned")).toBeInTheDocument();
    expect(screen.getByText("28 analysed beyond plan")).toBeInTheDocument();
    expect(screen.getByText("93.6M")).toBeInTheDocument();
    expect(screen.getByText(/across 1,117 model calls/)).toBeInTheDocument();
  });

  it("marks the endpoints analysed beyond the plan", () => {
    render(<DastRunPanels pipelineId="p1" />);
    expect(screen.getByText("Endpoints analysed (4)")).toBeInTheDocument();
    const marked = screen.getAllByTitle("Analysed beyond the run plan");
    expect(marked.map((element) => element.textContent)).toEqual(["cloud-prod-hdw-mx", "nxlicensed-hdw-mx"]);
  });

  it("filters the inventory and narrows it to the beyond-plan members", () => {
    render(<DastRunPanels pipelineId="p1" />);
    fireEvent.change(screen.getByLabelText("Filter analysed endpoints"), { target: { value: "auth" } });
    expect(screen.getByText("auth-test-hdw-mx")).toBeInTheDocument();
    expect(screen.queryByText("cloud-prod-hdw-mx")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Filter analysed endpoints"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Beyond plan only (2)" }));
    expect(screen.getByText("cloud-prod-hdw-mx")).toBeInTheDocument();
    expect(screen.queryByText("auth-test-hdw-mx")).not.toBeInTheDocument();
  });

  it("labels an unnamed phase by its number and switches to the agent breakdown", () => {
    render(<DastRunPanels pipelineId="p1" />);
    expect(screen.getByText("Phase 6 · depth: floor, explore, discovery")).toBeInTheDocument();
    expect(screen.getByText("Phase 4")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "By agent" }));
    expect(screen.getByText("dast-check-runner")).toBeInTheDocument();
    expect(screen.queryByText("Phase 4")).not.toBeInTheDocument();
  });

  it("flags an accounting mismatch without hiding the numbers", () => {
    detail = { ...FULL, tokenAccountingConsistent: false };
    render(<DastRunPanels pipelineId="p1" />);
    expect(screen.getByText(/Accounting mismatch/)).toBeInTheDocument();
    expect(screen.getByText("93.6M")).toBeInTheDocument();
  });
});

describe("optional blocks", () => {
  it("renders nothing at all for a pipeline with no accepted report", () => {
    detail = null;
    const { container } = render(<DastRunPanels pipelineId="p1" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("drops the coverage, inventory and spend panels when the report carried neither block", () => {
    detail = {
      ...FULL,
      runType: null,
      coverageUnit: null,
      discovered: null,
      reachable: null,
      analysed: null,
      planned: null,
      beyondPlan: null,
      totalTokens: null,
      modelCalls: null,
      durationSeconds: null,
      productFamily: null,
      tier: null,
      targetHost: null,
      analysedNames: null,
      beyondPlanNames: null,
      tokens: {
        input_tokens: null,
        output_tokens: null,
        thinking_tokens: null,
        cache_creation_tokens: null,
        cache_read_tokens: null,
      },
      tokenByPhase: null,
      tokenByAgentType: null,
      agents: null,
      tokenAccountingConsistent: null,
    };
    render(<DastRunPanels pipelineId="p1" />);
    // The run identity always survives; nothing else is invented.
    expect(screen.getByText("Run")).toBeInTheDocument();
    expect(screen.getByText("external-10host")).toBeInTheDocument();
    expect(screen.queryByText("Scan coverage")).not.toBeInTheDocument();
    expect(screen.queryByText("Agent token usage")).not.toBeInTheDocument();
    expect(screen.queryByText(/analysed \(/)).not.toBeInTheDocument();
    expect(screen.queryByText("—")).not.toBeInTheDocument();
  });

  it("keeps the coverage panel when only some stages were reported", () => {
    detail = { ...FULL, discovered: null, reachable: null, planned: null, beyondPlan: null };
    render(<DastRunPanels pipelineId="p1" />);
    expect(screen.getByText("Scan coverage")).toBeInTheDocument();
    expect(screen.getByText("Analysed")).toBeInTheDocument();
    expect(screen.queryByText("Discovered")).not.toBeInTheDocument();
    expect(screen.queryByText(/beyond plan/)).not.toBeInTheDocument();
  });
});
