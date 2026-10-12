import { describe, expect, it } from "vitest";

import {
  describePipelinesFilters,
  EMPTY_PIPELINES_FILTERS,
  type PipelinesFilterState,
} from "./pipelinesActiveFilters";

const PROJECT_NAMES: Record<number, string> = { 7: "Payments API" };
const STATUS_LABELS: Record<string, string> = { FINISHED_WITH_WARNINGS: "Finished With Warnings" };

const options = {
  projectName: (id: number) => PROJECT_NAMES[id],
  statusLabel: (value: string) => STATUS_LABELS[value],
};

function describe_(state: Partial<PipelinesFilterState>) {
  return describePipelinesFilters({ ...EMPTY_PIPELINES_FILTERS, ...state }, options);
}

/** Applying a chip's patch is how the page removes that chip; this mirrors the page. */
function apply(state: PipelinesFilterState, patch: Partial<PipelinesFilterState>): PipelinesFilterState {
  return { ...state, ...patch };
}

describe("describePipelinesFilters", () => {
  it("shows nothing when no filter is set", () => {
    expect(describe_({})).toEqual([]);
  });

  it("names the project and the status rather than echoing their raw values", () => {
    const chips = describe_({ projectId: 7, status: "FINISHED_WITH_WARNINGS" });

    expect(chips.map((chip) => [chip.label, chip.value])).toEqual([
      ["Project", "Payments API"],
      ["Status", "Finished With Warnings"],
    ]);
  });

  it("falls back to the identifier when a project is not among the ones loaded", () => {
    expect(describe_({ projectId: 404 })[0].value).toBe("#404");
  });

  it("treats the default status as no filter at all", () => {
    expect(describe_({ status: "all" })).toEqual([]);
  });

  it("reads a half-open created range as from or until", () => {
    expect(describe_({ createdFrom: "2026-10-01" })[0].value).toBe("from 2026-10-01");
    expect(describe_({ createdTo: "2026-10-06" })[0].value).toBe("until 2026-10-06");
    expect(describe_({ createdFrom: "2026-10-01", createdTo: "2026-10-06" })[0].value)
      .toBe("2026-10-01 – 2026-10-06");
  });

  it("removes exactly the chip that was dismissed and leaves the others alone", () => {
    const state: PipelinesFilterState = {
      projectId: 7,
      status: "FINISHED_WITH_WARNINGS",
      search: "release-v1",
      createdFrom: "2026-10-01",
      createdTo: "2026-10-06",
    };
    const chips = describePipelinesFilters(state, options);
    const searchChip = chips.find((chip) => chip.id === "search");
    expect(searchChip).toBeDefined();

    const next = apply(state, searchChip!.patch);

    expect(next.search).toBe("");
    expect(describePipelinesFilters(next, options).map((chip) => chip.id)).toEqual([
      "project",
      "status",
      "created",
    ]);
  });

  it("clears both ends of the range from the single created chip", () => {
    const state: PipelinesFilterState = {
      ...EMPTY_PIPELINES_FILTERS,
      createdFrom: "2026-10-01",
      createdTo: "2026-10-06",
    };
    const chip = describePipelinesFilters(state, options)[0];

    expect(describePipelinesFilters(apply(state, chip.patch), options)).toEqual([]);
  });
});
