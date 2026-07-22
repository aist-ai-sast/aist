import { describe, expect, it } from "vitest";

import type { Finding } from "../types";
import { isDastFinding } from "./findingStatus";

describe("isDastFinding", () => {
  it("is true when dynamicFinding is set", () => {
    expect(isDastFinding({ dynamicFinding: true } as Finding)).toBe(true);
  });

  it("is true when the dast tag is present even without dynamicFinding", () => {
    expect(isDastFinding({ tags: ["dast"] } as Finding)).toBe(true);
  });

  it("is false for a plain SAST finding", () => {
    expect(isDastFinding({ tags: ["sast"] } as Finding)).toBe(false);
  });
});
