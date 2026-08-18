import { describe, expect, it } from "vitest";

import {
  bucketLabel,
  bucketShare,
  bucketsBySpend,
  formatCompactTokens,
  formatCount,
  formatDuration,
  formatShare,
} from "./dastRun";
import type { DastTokenBucket } from "../types";

// Numbers below are the real ones from run 80c744a2be37d91c07a7a8ef97c520be.
function bucket(overrides: Partial<DastTokenBucket> & { key: string }): DastTokenBucket {
  return { total_tokens: null, ...overrides };
}

describe("optional values never render as a zero", () => {
  it("returns null instead of substituting a number the report did not carry", () => {
    expect(formatCount(null)).toBeNull();
    expect(formatCount(undefined)).toBeNull();
    expect(formatCompactTokens(null)).toBeNull();
    expect(formatDuration(null)).toBeNull();
    expect(formatShare(null)).toBeNull();
  });

  it("keeps a genuine zero, which is a reported fact rather than an absence", () => {
    expect(formatCount(0)).toBe("0");
    expect(formatCompactTokens(0)).toBe("0");
    expect(formatDuration(0)).toBe("0s");
  });
});

describe("token formatting", () => {
  it("compacts the headline and keeps the detail exact", () => {
    expect(formatCompactTokens(93_556_484)).toBe("93.6M");
    expect(formatCompactTokens(951_808)).toBe("952k");
    expect(formatCompactTokens(1_117)).toBe("1k");
    expect(formatCompactTokens(999)).toBe("999");
    expect(formatCount(90_024_238)).toBe("90,024,238");
  });
});

describe("duration", () => {
  it("formats the real 2h 18m 49s run", () => {
    expect(formatDuration(8_329)).toBe("2h 18m 49s");
  });

  it("drops empty leading units", () => {
    expect(formatDuration(49)).toBe("49s");
    expect(formatDuration(125)).toBe("2m 5s");
  });
});

describe("bucket labels", () => {
  it("names an unnamed phase by its number instead of showing a blank", () => {
    expect(bucketLabel(bucket({ key: "4" }), "phase")).toBe("Phase 4");
    expect(bucketLabel(bucket({ key: "2", name: null }), "phase")).toBe("Phase 2");
  });

  it("combines number and name when the phase carries one", () => {
    expect(bucketLabel(bucket({ key: "7", name: "verify" }), "phase")).toBe("Phase 7 · verify");
  });

  it("does not prefix a non-numeric phase key", () => {
    expect(bucketLabel(bucket({ key: "unattributed", name: "unattributed" }), "phase")).toBe("unattributed");
  });

  it("uses the agent type's own key", () => {
    expect(bucketLabel(bucket({ key: "dast-check-runner", agents: 14 }), "agent")).toBe("dast-check-runner");
  });
});

describe("shares", () => {
  it("computes a bucket's share of the run total", () => {
    const phase = bucket({ key: "6", total_tokens: 56_824_360 });
    expect(formatShare(bucketShare(phase, 93_556_484))).toBe("60.7%");
  });

  it("reports no share when either side went unreported", () => {
    expect(bucketShare(bucket({ key: "6" }), 93_556_484)).toBeNull();
    expect(bucketShare(bucket({ key: "6", total_tokens: 10 }), null)).toBeNull();
    expect(bucketShare(bucket({ key: "6", total_tokens: 10 }), 0)).toBeNull();
  });
});

describe("ordering", () => {
  it("opens on where the run actually spent", () => {
    const ordered = bucketsBySpend([
      bucket({ key: "3", total_tokens: 655_787 }),
      bucket({ key: "6", total_tokens: 56_824_360 }),
      bucket({ key: "7", total_tokens: 17_320_070 }),
    ]);
    expect(ordered.map((item) => item.key)).toEqual(["6", "7", "3"]);
  });

  it("treats an absent list as empty rather than throwing", () => {
    expect(bucketsBySpend(null)).toEqual([]);
  });
});
