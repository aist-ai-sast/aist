// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { ApiError, toUserMessage } from "./api";

describe("toUserMessage", () => {
  it("joins DRF field-level validation errors into a readable message", () => {
    // {"new_password": [...]} is what AISTSetPasswordSerializer now raises for
    // a weak password — previously this fell through to a generic
    // "Request failed with status code 400" because extractErrorDetail only
    // understood a top-level array or a "detail" key.
    const error = new ApiError({
      status: 400,
      code: "http_error",
      payload: { new_password: ["This password is too short. It must contain at least 8 characters."] },
      url: "/api/v2/aist/auth/set-password/",
    });
    expect(toUserMessage(error)).toBe("This password is too short. It must contain at least 8 characters.");
  });

  it("joins messages across multiple fields", () => {
    const error = new ApiError({
      status: 400,
      code: "http_error",
      payload: { new_password: ["Too short."], new_password_confirm: ["Does not match."] },
      url: "/api/v2/aist/auth/set-password/",
    });
    const message = toUserMessage(error);
    expect(message).toContain("Too short.");
    expect(message).toContain("Does not match.");
  });

  it("still prefers a top-level detail string when present", () => {
    const error = new ApiError({
      status: 400,
      code: "http_error",
      payload: { detail: "Bad input" },
      url: "/x",
    });
    expect(toUserMessage(error)).toBe("Bad input");
  });

  it("surfaces the duplicate-token-name message instead of a bare failure", () => {
    // {"name": ["You already have a token with this name."]} is exactly what
    // AISTApiTokenCreateSerializer.validate_name raises — MyTokensSection's
    // create-token handler passes the caught error straight to toUserMessage.
    const error = new ApiError({
      status: 400,
      code: "http_error",
      payload: { name: ["You already have a token with this name."] },
      url: "/api/v2/aist/me/tokens/",
    });
    expect(toUserMessage(error)).toBe("You already have a token with this name.");
  });

  it("surfaces a DRF field error whose value is a bare string, not a list", () => {
    // {"role_id": "Cannot grant a project role higher than the member's
    // organization role."} is exactly what _grant_project's ValidationError
    // raises (aist/members/service.py) — DRF only wraps the message in a
    // list when the raiser passes one, so this arrives on the wire as a
    // plain string, not ["..."]. Previously this fell through to a generic
    // "Request failed with status code 400" instead of the real reason.
    const error = new ApiError({
      status: 400,
      code: "http_error",
      payload: { role_id: "Cannot grant a project role higher than the member's organization role." },
      url: "/api/v2/aist/organizations/1/members/2/project-grants/",
    });
    expect(toUserMessage(error)).toBe("Cannot grant a project role higher than the member's organization role.");
  });

  it("joins DRF field errors with mixed string and list values", () => {
    const error = new ApiError({
      status: 400,
      code: "http_error",
      payload: { role_id: "Unknown role.", project_id: ["Project does not belong to this organization."] },
      url: "/x",
    });
    const message = toUserMessage(error);
    expect(message).toContain("Unknown role.");
    expect(message).toContain("Project does not belong to this organization.");
  });

  it("falls back to a generic message for a 5xx error", () => {
    const error = new ApiError({ status: 500, code: "http_error", payload: null, url: "/x" });
    expect(toUserMessage(error)).toBe("Server error. Please try again later.");
  });

  it("never surfaces the backend payload for an auth_expired error, even if it contains internal detail", () => {
    const error = new ApiError({
      status: 401,
      code: "auth_expired",
      payload: { detail: "user_id=42 org_id=17 session token abcdef123" },
      url: "/api/v2/aist/me/",
    });
    const message = toUserMessage(error);
    expect(message).toBe("Session expired, please login again.");
    expect(message).not.toContain("user_id");
    expect(message).not.toContain("org_id");
  });

  it("never surfaces the backend payload for a forbidden error, even if it names another org/user", () => {
    const error = new ApiError({
      status: 403,
      code: "forbidden",
      payload: { detail: "You do not have access to organization 'Acme Internal' (id=99)" },
      url: "/api/v2/aist/organizations/99/members/",
    });
    const message = toUserMessage(error);
    expect(message).toBe("Access denied.");
    expect(message).not.toContain("Acme Internal");
    expect(message).not.toContain("99");
  });
});
