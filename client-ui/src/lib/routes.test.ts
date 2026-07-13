import { describe, expect, it } from "vitest";

import { getCsrfToken } from "./api";
import { getRoute } from "./routes";

describe("node-safe route bootstrap", () => {
  it("throws a controlled error instead of crashing on import when window is unavailable", () => {
    expect(() => getRoute("me_url")).toThrowError("Routes are not available.");
  });

  it("returns null csrf token when window and document are unavailable", () => {
    expect(getCsrfToken()).toBeNull();
  });
});
