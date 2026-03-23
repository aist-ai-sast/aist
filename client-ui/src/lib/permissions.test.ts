// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach } from "vitest";
import type { UserProfile } from "./auth";

vi.mock("./auth", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./auth")>();
  return { ...actual, useAuthStatus: vi.fn() };
});

import { useAuthStatus } from "./auth";
import { usePermissions } from "./permissions";
import { renderHook } from "@testing-library/react";

const mockAuth = (profile: UserProfile | null) => {
  (useAuthStatus as ReturnType<typeof vi.fn>).mockReturnValue({
    data: profile,
    isLoading: false,
  });
};

// Role IDs as used in permissions.ts
const RoleIds = { Reader: 5, API_Importer: 1, Writer: 2, Maintainer: 3, Owner: 4 };

describe("usePermissions - canManageAccess", () => {
  beforeEach(() => vi.clearAllMocks());

  it("returns false for unauthenticated user", () => {
    mockAuth(null);
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canManageAccess).toBe(false);
  });

  it("returns true for superuser", () => {
    mockAuth({ user: { username: "admin", is_superuser: true } });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canManageAccess).toBe(true);
  });

  it("returns true for Organization Owner (product_type_member with Owner role)", () => {
    mockAuth({
      user: { username: "org_owner" },
      product_type_member: [{ product_type: 10, role: RoleIds.Owner }],
    });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canManageAccess).toBe(true);
  });

  it("returns true for Organization Maintainer (product_type_member with Maintainer role)", () => {
    mockAuth({
      user: { username: "org_maintainer" },
      product_type_member: [{ product_type: 10, role: RoleIds.Maintainer }],
    });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canManageAccess).toBe(true);
  });

  it("returns false for Organization Reader (product_type_member with Reader role)", () => {
    mockAuth({
      user: { username: "org_reader" },
      product_type_member: [{ product_type: 10, role: RoleIds.Reader }],
    });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canManageAccess).toBe(false);
  });

  it("returns false for Organization Writer (product_type_member with Writer role)", () => {
    mockAuth({
      user: { username: "org_writer" },
      product_type_member: [{ product_type: 10, role: RoleIds.Writer }],
    });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canManageAccess).toBe(false);
  });

  it("returns true when at least one org membership is Owner", () => {
    mockAuth({
      user: { username: "multi_org" },
      product_type_member: [
        { product_type: 10, role: RoleIds.Reader },
        { product_type: 11, role: RoleIds.Owner },
      ],
    });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canManageAccess).toBe(true);
  });

  it("returns true for global Maintainer role (no org membership)", () => {
    mockAuth({
      user: { username: "global_maint" },
      global_role: { role: RoleIds.Maintainer },
    });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canManageAccess).toBe(true);
  });

  it("org membership takes precedence over global_role", () => {
    // org is Reader, global is Owner — org membership wins and blocks access
    mockAuth({
      user: { username: "mixed" },
      product_type_member: [{ product_type: 10, role: RoleIds.Reader }],
      global_role: { role: RoleIds.Owner },
    });
    const { result } = renderHook(() => usePermissions());
    // product_type_member is present so global_role is ignored; Reader → false
    expect(result.current.canManageAccess).toBe(false);
  });
});

describe("usePermissions - canWrite", () => {
  beforeEach(() => vi.clearAllMocks());

  it("returns true for Organization Writer", () => {
    mockAuth({
      user: { username: "org_writer" },
      product_type_member: [{ product_type: 10, role: RoleIds.Writer }],
    });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canWrite).toBe(true);
  });

  it("returns true for Organization Owner", () => {
    mockAuth({
      user: { username: "org_owner" },
      product_type_member: [{ product_type: 10, role: RoleIds.Owner }],
    });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canWrite).toBe(true);
  });

  it("returns false for Organization Reader", () => {
    mockAuth({
      user: { username: "org_reader" },
      product_type_member: [{ product_type: 10, role: RoleIds.Reader }],
    });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canWrite).toBe(false);
  });
});
