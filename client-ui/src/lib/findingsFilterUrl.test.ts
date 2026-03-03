import { describe, expect, it } from "vitest";

import { buildFindingsFilterSearch, parseFindingsFiltersFromSearch, toFindingsApiFilters } from "./findingsFilterUrl";

describe("parseFindingsFiltersFromSearch", () => {
  it("parses filters from query aliases", () => {
    const params = new URLSearchParams({
      project: "17",
      pipeline_id: "pipe-1",
      created_gte: "2026-03-01",
      created_lte: "2026-03-05",
      processed_gte: "2026-03-02",
      processed_lte: "2026-03-03",
      mitigated_gte: "2026-03-01",
      mitigated_lte: "2026-03-02",
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
      createdFrom: "2026-03-01",
      createdTo: "2026-03-05",
      statusUpdatedFrom: "2026-03-02",
      statusUpdatedTo: "2026-03-03",
      mitigatedFrom: "2026-03-01",
      mitigatedTo: "2026-03-02",
      projectVersion: "master",
      file: "src/app.ts",
      cwe: "79,89",
      severities: ["Critical", "High"],
      tags: ["api", "auth"],
      status: "Non-Active",
      risk: ["risk_accepted", "under_review"],
      aiStatus: "ai_tp",
    });
  });
});

describe("buildFindingsFilterSearch", () => {
  it("builds canonical query params for non-empty filters", () => {
    const query = buildFindingsFilterSearch({
      projectId: 42,
      pipelineId: "abc",
      createdFrom: "2026-03-01",
      createdTo: "2026-03-02",
      statusUpdatedFrom: "2026-03-03",
      statusUpdatedTo: "2026-03-04",
      mitigatedFrom: "2026-03-05",
      mitigatedTo: "2026-03-06",
      projectVersion: "release",
      file: "src/main.ts",
      cwe: "79",
      severities: ["High", "Critical"],
      tags: ["b", "a"],
      status: "Active",
      risk: ["mitigated", "risk_accepted"],
      aiStatus: "ai_u",
    });

    expect(query.toString()).toBe(
      "project_id=42&pipeline_id=abc&created_gte=2026-03-01&created_lte=2026-03-02&processed_gte=2026-03-03&processed_lte=2026-03-04&mitigated_gte=2026-03-05&mitigated_lte=2026-03-06&project_version=release&file=src%2Fmain.ts&cwe=79&severity=Critical%2CHigh&tags=a%2Cb&active=true&risk_accepted=true&is_mitigated=true&ai_status=ai_u",
    );
  });
});

describe("toFindingsApiFilters", () => {
  it("maps ui state to api filter contract", () => {
    const filters = toFindingsApiFilters(
      {
        projectId: 11,
        pipelineId: "p-1",
        createdFrom: "2026-03-01",
        createdTo: "2026-03-02",
        statusUpdatedFrom: "2026-03-03",
        statusUpdatedTo: "2026-03-04",
        mitigatedFrom: "2026-03-05",
        mitigatedTo: "2026-03-06",
        projectVersion: "main",
        file: "src/a.ts",
        cwe: "79",
        severities: ["High"],
        tags: ["tag-1"],
        status: "Non-Active",
        risk: ["mitigated"],
        aiStatus: "ai_fp",
      },
      { limit: 25, offset: 50, ordering: "-severity" },
    );

    expect(filters).toEqual({
      projectId: 11,
      pipelineId: "p-1",
      createdGte: "2026-03-01",
      createdLte: "2026-03-02",
      statusUpdatedGte: "2026-03-03",
      statusUpdatedLte: "2026-03-04",
      processedGte: "2026-03-03",
      processedLte: "2026-03-04",
      mitigatedGte: "2026-03-05",
      mitigatedLte: "2026-03-06",
      projectVersion: "main",
      file: "src/a.ts",
      aiStatus: "ai_fp",
      severities: ["High"],
      status: "disabled",
      riskStates: ["mitigated"],
      cwe: "79",
      tags: ["tag-1"],
      limit: 25,
      offset: 50,
      ordering: "-severity",
    });
  });
});
