import { describe, expect, it } from "vitest";

import { agingBucketRange, drilldownSearch, weekRange } from "./dashboardDrilldown";
import { DEFAULT_FINDINGS_FILTERS, type FindingsFilterUrlState } from "./findingsFilterUrl";

const DASHBOARD_FILTER: FindingsFilterUrlState = {
  ...DEFAULT_FINDINGS_FILTERS,
  projectId: 5,
  severities: ["Critical", "High"],
  tags: { include: ["auth"], exclude: ["test"], matchMode: "any" },
  createdFrom: "2026-01-01",
};

describe("drilldownSearch", () => {
  it("keeps the dashboard filter and narrows to the clicked severity", () => {
    const params = new URLSearchParams(drilldownSearch(DASHBOARD_FILTER, { severities: ["High"], status: "Active" }));

    expect(params.get("severity")).toBe("High");
    expect(params.get("active")).toBe("true");
    expect(params.get("project_id")).toBe("5");
    expect(params.get("tags")).toBe("auth");
    expect(params.get("not_tags")).toBe("test");
    expect(params.get("created_gte")).toBe("2026-01-01");
  });

  it("opens the dashboard selection as is without a slice", () => {
    const params = new URLSearchParams(drilldownSearch(DASHBOARD_FILTER));

    expect(params.get("severity")).toBe("Critical,High");
    expect(params.get("active")).toBeNull();
  });

  it("switches to non-active findings for the mitigated trend line", () => {
    const params = new URLSearchParams(
      drilldownSearch(DASHBOARD_FILTER, { ...weekRange("2026-09-21"), status: "Non-Active" }),
    );

    expect(params.get("active")).toBe("false");
    expect(params.get("created_gte")).toBe("2026-09-21");
    expect(params.get("created_lte")).toBe("2026-09-27");
  });

  it("replaces the user's created range with the aging bucket", () => {
    const now = new Date("2026-09-25T12:00:00Z");
    const params = new URLSearchParams(
      drilldownSearch(DASHBOARD_FILTER, { severities: ["High"], ...agingBucketRange("8_30", now) }),
    );

    expect(params.get("created_gte")).toBe("2026-08-26");
    expect(params.get("created_lte")).toBe("2026-09-17");
  });
});

describe("agingBucketRange", () => {
  const now = new Date("2026-09-25T12:00:00Z");

  it("maps each bucket to a range that does not overlap its neighbours", () => {
    expect(agingBucketRange("0_7", now)).toEqual({ createdFrom: "2026-09-18", createdTo: "2026-09-25" });
    expect(agingBucketRange("8_30", now)).toEqual({ createdFrom: "2026-08-26", createdTo: "2026-09-17" });
    expect(agingBucketRange("31_90", now)).toEqual({ createdFrom: "2026-06-27", createdTo: "2026-08-25" });
    expect(agingBucketRange("90_plus", now)).toEqual({ createdFrom: "", createdTo: "2026-06-26" });
  });
});
