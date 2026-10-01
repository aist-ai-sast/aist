import { describe, expect, it } from "vitest";

import {
  buildFindingsFilterSearch,
  DEFAULT_FINDINGS_FILTERS,
  parseFindingsFiltersFromSearch,
  toFindingsApiFilters,
} from "./findingsFilterUrl";

describe("parseFindingsFiltersFromSearch", () => {
  it("parses filters from query aliases", () => {
    const params = new URLSearchParams({
      project: "17",
      pipeline_id: "pipe-1",
      title: "SQL Injection",
      created_gte: "2026-03-01",
      created_lte: "2026-03-05",
      processed_gte: "2026-03-02",
      processed_lte: "2026-03-03",
      mitigated_gte: "2026-03-01",
      mitigated_lte: "2026-03-02",
      work_item_linked_gte: "2026-03-07",
      work_item_linked_lte: "2026-03-08",
      project_version: "master",
      file: "src/app.ts",
      cwe: "79,89",
      severity: "High,Critical",
      tags: "api,auth",
      active: "false",
      risk_accepted: "true",
      under_review: "true",
      ai_status: "ai_tp",
    });

    const parsed = parseFindingsFiltersFromSearch(params);

    expect(parsed).toEqual({
      projectId: 17,
      pipelineId: "pipe-1",
      title: "SQL Injection",
      createdFrom: "2026-03-01",
      createdTo: "2026-03-05",
      statusUpdatedFrom: "2026-03-02",
      statusUpdatedTo: "2026-03-03",
      mitigatedFrom: "2026-03-01",
      mitigatedTo: "2026-03-02",
      workItemLinkedFrom: "2026-03-07",
      workItemLinkedTo: "2026-03-08",
      projectVersion: "master",
      file: "src/app.ts",
      cwe: "79,89",
      severities: ["Critical", "High"],
      tags: { include: ["api", "auth"], exclude: [], matchMode: "any" },
      status: "Non-Active",
      risk: ["risk_accepted", "under_review"],
      aiStatus: "ai_tp",
      workItemStatus: "all",
    });
  });
});

describe("parseFindingsFiltersFromSearch — work_item_status", () => {
  it("parses work_item_status directly", () => {
    const params = new URLSearchParams({ work_item_status: "OPEN" });
    expect(parseFindingsFiltersFromSearch(params).workItemStatus).toBe("OPEN");
  });

  it("maps legacy has_work_item=yes to any", () => {
    const params = new URLSearchParams({ has_work_item: "yes" });
    expect(parseFindingsFiltersFromSearch(params).workItemStatus).toBe("any");
  });

  it("maps legacy has_work_item=no to none", () => {
    const params = new URLSearchParams({ has_work_item: "no" });
    expect(parseFindingsFiltersFromSearch(params).workItemStatus).toBe("none");
  });

  it("defaults to all when param is absent", () => {
    const params = new URLSearchParams({});
    expect(parseFindingsFiltersFromSearch(params).workItemStatus).toBe("all");
  });
});

describe("buildFindingsFilterSearch", () => {
  it("builds canonical query params for non-empty filters", () => {
    const query = buildFindingsFilterSearch({
      projectId: 42,
      pipelineId: "abc",
      title: "XSS",
      createdFrom: "2026-03-01",
      createdTo: "2026-03-02",
      statusUpdatedFrom: "2026-03-03",
      statusUpdatedTo: "2026-03-04",
      mitigatedFrom: "2026-03-05",
      mitigatedTo: "2026-03-06",
      workItemLinkedFrom: "2026-03-07",
      workItemLinkedTo: "2026-03-08",
      projectVersion: "release",
      file: "src/main.ts",
      cwe: "79",
      severities: ["High", "Critical"],
      tags: { include: ["b", "a"], exclude: [], matchMode: "any" },
      status: "Active",
      risk: ["mitigated", "risk_accepted"],
      aiStatus: "ai_u",
    });

    expect(query.toString()).toBe(
      "project_id=42&pipeline_id=abc&title=XSS&created_gte=2026-03-01&created_lte=2026-03-02&processed_gte=2026-03-03&processed_lte=2026-03-04&mitigated_gte=2026-03-05&mitigated_lte=2026-03-06&work_item_linked_gte=2026-03-07&work_item_linked_lte=2026-03-08&project_version=release&file=src%2Fmain.ts&cwe=79&severity=Critical%2CHigh&tags=a%2Cb&active=true&risk_accepted=true&is_mitigated=true&ai_status=ai_u",
    );
  });
});

describe("tag filter url state", () => {
  it("parses include, exclude and all-of from the api-shaped params", () => {
    const parsed = parseFindingsFiltersFromSearch(new URLSearchParams({
      tags__and: "dast,cve",
      not_tags: "inconclusive, dast",
    }));
    // a tag cannot be both included and excluded; include wins
    expect(parsed.tags).toEqual({ include: ["cve", "dast"], exclude: ["inconclusive"], matchMode: "all" });
  });

  it("keeps legacy tags= links working as any-of", () => {
    const parsed = parseFindingsFiltersFromSearch(new URLSearchParams({ tags: "dast" }));
    expect(parsed.tags).toEqual({ include: ["dast"], exclude: [], matchMode: "any" });
  });

  it("serialises the dast-but-not-inconclusive scenario", () => {
    const query = buildFindingsFilterSearch({
      ...DEFAULT_FINDINGS_FILTERS,
      tags: { include: ["dast"], exclude: ["inconclusive"], matchMode: "any" },
    });
    expect(query.toString()).toBe("tags=dast&not_tags=inconclusive");

    const allOf = buildFindingsFilterSearch({
      ...DEFAULT_FINDINGS_FILTERS,
      tags: { include: ["dast", "cve"], exclude: [], matchMode: "all" },
    });
    expect(allOf.toString()).toBe("tags__and=cve%2Cdast");
  });

  it("round-trips through parse and build", () => {
    const state = { ...DEFAULT_FINDINGS_FILTERS, tags: { include: ["a"], exclude: ["b", "c"], matchMode: "all" as const } };
    expect(parseFindingsFiltersFromSearch(buildFindingsFilterSearch(state)).tags).toEqual(state.tags);
  });

  it("omits tag params from the api contract when nothing is selected", () => {
    const filters = toFindingsApiFilters(DEFAULT_FINDINGS_FILTERS);
    expect(filters.tags).toBeUndefined();
    expect(filters.tagMatchMode).toBeUndefined();
    expect(filters.excludedTags).toBeUndefined();
  });
});

describe("toFindingsApiFilters", () => {
  it("maps ui state to api filter contract", () => {
    const filters = toFindingsApiFilters(
      {
        projectId: 11,
        pipelineId: "p-1",
        title: "Path Traversal",
        createdFrom: "2026-03-01",
        createdTo: "2026-03-02",
        statusUpdatedFrom: "2026-03-03",
        statusUpdatedTo: "2026-03-04",
        mitigatedFrom: "2026-03-05",
        mitigatedTo: "2026-03-06",
        workItemLinkedFrom: "2026-03-07",
        workItemLinkedTo: "",
        projectVersion: "main",
        file: "src/a.ts",
        cwe: "79",
        severities: ["High"],
        tags: { include: ["tag-1"], exclude: ["tag-2"], matchMode: "all" },
        status: "Non-Active",
        risk: ["mitigated"],
        aiStatus: "ai_fp",
        workItemStatus: "all",
      },
      { limit: 25, offset: 50, ordering: "-severity" },
    );

    expect(filters).toEqual({
      projectId: 11,
      pipelineId: "p-1",
      title: "Path Traversal",
      createdGte: "2026-03-01",
      createdLte: "2026-03-02",
      statusUpdatedGte: "2026-03-03",
      statusUpdatedLte: "2026-03-04",
      processedGte: "2026-03-03",
      processedLte: "2026-03-04",
      mitigatedGte: "2026-03-05",
      mitigatedLte: "2026-03-06",
      workItemLinkedGte: "2026-03-07",
      workItemLinkedLte: undefined,
      projectVersion: "main",
      file: "src/a.ts",
      aiStatus: "ai_fp",
      workItemStatus: undefined,
      severities: ["High"],
      status: "disabled",
      riskStates: ["mitigated"],
      cwe: "79",
      tags: ["tag-1"],
      tagMatchMode: "all",
      excludedTags: ["tag-2"],
      limit: 25,
      offset: 50,
      ordering: "-severity",
    });
  });
});
