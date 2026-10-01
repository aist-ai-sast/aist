import { describe, expect, it } from "vitest";

import { describeActiveFilters } from "./findingsActiveFilters";
import { DEFAULT_FINDINGS_FILTERS, type FindingsFilterUrlState } from "./findingsFilterUrl";

describe("describeActiveFilters", () => {
  it("returns nothing for the default state", () => {
    expect(describeActiveFilters(DEFAULT_FINDINGS_FILTERS)).toEqual([]);
  });

  it("describes the dast-but-not-inconclusive view and removes chips one at a time", () => {
    const state: FindingsFilterUrlState = {
      ...DEFAULT_FINDINGS_FILTERS,
      status: "Active",
      tags: { include: ["dast"], exclude: ["inconclusive"], matchMode: "any" },
    };
    const chips = describeActiveFilters(state);

    expect(chips.map((chip) => [chip.label, chip.value, chip.tone])).toEqual([
      ["Status", "Active", "neutral"],
      ["Tag", "dast", "include"],
      ["Not tag", "inconclusive", "exclude"],
    ]);

    const withoutExclude = chips.find((chip) => chip.id === "tag:exclude:inconclusive")!;
    expect(withoutExclude.patch).toEqual({ tags: { include: ["dast"], exclude: [], matchMode: "any" } });
  });

  it("labels all-of tag chips and resolves project names", () => {
    const state: FindingsFilterUrlState = {
      ...DEFAULT_FINDINGS_FILTERS,
      projectId: 7,
      projectVersion: "release-1",
      severities: ["Critical", "High"],
      tags: { include: ["a", "b"], exclude: [], matchMode: "all" },
      aiStatus: "ai_tp",
      workItemStatus: "OPEN",
      createdFrom: "2026-01-01",
      createdTo: "",
    };
    const chips = describeActiveFilters(state, { projectName: (id) => (id === 7 ? "Payments API" : undefined) });
    const byId = Object.fromEntries(chips.map((chip) => [chip.id, chip]));

    expect(byId.project.value).toBe("Payments API");
    // dropping the project also drops the project-scoped version
    expect(byId.project.patch).toEqual({ projectId: undefined, projectVersion: "" });
    expect(byId["tag:include:a"].label).toBe("All tags");
    expect(byId["severity:High"].patch).toEqual({ severities: ["Critical"] });
    expect(byId.ai_status.value).toBe("AI TP");
    expect(byId.work_item.value).toBe("Open");
    expect(byId.created.value).toBe("from 2026-01-01");
  });

  it("shows the ticketed range a dashboard click applied and removes it as one chip", () => {
    const chips = describeActiveFilters({
      ...DEFAULT_FINDINGS_FILTERS,
      workItemStatus: "OPEN",
      workItemLinkedTo: "2026-09-01",
    });
    const ticketed = chips.find((chip) => chip.id === "work_item_linked")!;

    expect([ticketed.label, ticketed.value]).toEqual(["Ticketed", "until 2026-09-01"]);
    expect(ticketed.patch).toEqual({ workItemLinkedFrom: "", workItemLinkedTo: "" });
  });
});
