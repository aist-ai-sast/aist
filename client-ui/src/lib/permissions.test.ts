// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach } from "vitest";
import type { OrganizationMembership, UserProfile } from "./auth";

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

const membership = (roleId: number, orgId = 10): OrganizationMembership => ({
  organization_id: orgId,
  organization_name: `Org ${orgId}`,
  role_id: roleId,
  role_name: "role",
});

describe("usePermissions - canManageAccess", () => {
  beforeEach(() => vi.clearAllMocks());

  it("returns false for unauthenticated user", () => {
    mockAuth(null);
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canManageAccess).toBe(false);
  });

  it("returns true for superuser", () => {
    mockAuth({ username: "admin", is_superuser: true });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canManageAccess).toBe(true);
  });

  it("returns true for Organization Owner", () => {
    mockAuth({
      username: "org_owner",
      organization_memberships: [membership(RoleIds.Owner)],
    });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canManageAccess).toBe(true);
  });

  it("returns true for Organization Maintainer", () => {
    mockAuth({
      username: "org_maintainer",
      organization_memberships: [membership(RoleIds.Maintainer)],
    });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canManageAccess).toBe(true);
  });

  it("returns false for Organization Reader", () => {
    mockAuth({
      username: "org_reader",
      organization_memberships: [membership(RoleIds.Reader)],
    });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canManageAccess).toBe(false);
  });

  it("returns false for Organization Writer", () => {
    mockAuth({
      username: "org_writer",
      organization_memberships: [membership(RoleIds.Writer)],
    });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canManageAccess).toBe(false);
  });

  it("returns true when at least one org membership is Owner", () => {
    mockAuth({
      username: "multi_org",
      organization_memberships: [membership(RoleIds.Reader, 10), membership(RoleIds.Owner, 11)],
    });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canManageAccess).toBe(true);
  });
});

describe("usePermissions - canWrite", () => {
  beforeEach(() => vi.clearAllMocks());

  it("returns true for Organization Writer", () => {
    mockAuth({
      username: "org_writer",
      organization_memberships: [membership(RoleIds.Writer)],
    });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canWrite).toBe(true);
  });

  it("returns true for Organization Owner", () => {
    mockAuth({
      username: "org_owner",
      organization_memberships: [membership(RoleIds.Owner)],
    });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canWrite).toBe(true);
  });

  it("returns false for Organization Reader", () => {
    mockAuth({
      username: "org_reader",
      organization_memberships: [membership(RoleIds.Reader)],
    });
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canWrite).toBe(false);
  });
});
