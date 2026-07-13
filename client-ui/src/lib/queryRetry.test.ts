import { describe, expect, it } from "vitest";

import { ApiError } from "./api";
import { shouldRetry } from "./queryRetry";

describe("shouldRetry", () => {
  it("never retries a 400 — it's a deterministic validation failure", () => {
    // Regression: this used to retry twice (~3s of backoff) before
    // rejecting, during which any "busy" state derived from isPending
    // stayed true — e.g. the Manage-access drawer's row selectors — which
    // read as a UI hang for a request that was never going to succeed.
    const error = new ApiError({ status: 400, code: "http_error", payload: null, url: "/x" });
    expect(shouldRetry(0, error)).toBe(false);
    expect(shouldRetry(1, error)).toBe(false);
  });

  it("never retries a 404 or 409", () => {
    expect(shouldRetry(0, new ApiError({ status: 404, code: "http_error", payload: null, url: "/x" }))).toBe(false);
    expect(shouldRetry(0, new ApiError({ status: 409, code: "http_error", payload: null, url: "/x" }))).toBe(false);
  });

  it("never retries auth_expired or forbidden", () => {
    expect(shouldRetry(0, new ApiError({ status: 401, code: "auth_expired", payload: null, url: "/x" }))).toBe(false);
    expect(shouldRetry(0, new ApiError({ status: 403, code: "forbidden", payload: null, url: "/x" }))).toBe(false);
  });

  it("retries a 500 up to twice", () => {
    const error = new ApiError({ status: 500, code: "http_error", payload: null, url: "/x" });
    expect(shouldRetry(0, error)).toBe(true);
    expect(shouldRetry(1, error)).toBe(true);
    expect(shouldRetry(2, error)).toBe(false);
  });

  it("retries a non-ApiError (e.g. network failure) up to twice", () => {
    const error = new Error("Network Error");
    expect(shouldRetry(0, error)).toBe(true);
    expect(shouldRetry(2, error)).toBe(false);
  });
});
