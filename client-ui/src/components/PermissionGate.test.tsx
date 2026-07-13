// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi, beforeEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

vi.mock("../lib/permissions", () => ({
  usePermissions: vi.fn(),
}));

import { usePermissions } from "../lib/permissions";
import PermissionGate from "./PermissionGate";

afterEach(() => {
  cleanup();
});

const mockPerms = (over: Partial<ReturnType<typeof usePermissions>>) => {
  (usePermissions as ReturnType<typeof vi.fn>).mockReturnValue({
    canWrite: false,
    canComment: false,
    canEnable: false,
    canExport: true,
    canManageAccess: false,
    isLoading: false,
    ...over,
  });
};

describe("PermissionGate manage_access", () => {
  beforeEach(() => vi.clearAllMocks());

  it("hides children from a user who cannot manage access (e.g. Reader)", () => {
    mockPerms({ canManageAccess: false });
    render(
      <PermissionGate action="manage_access">
        <button>Manage members</button>
      </PermissionGate>,
    );
    expect(screen.queryByText("Manage members")).toBeNull();
  });

  it("shows children to a manager (Owner/Maintainer/superuser)", () => {
    mockPerms({ canManageAccess: true });
    render(
      <PermissionGate action="manage_access">
        <button>Manage members</button>
      </PermissionGate>,
    );
    expect(screen.getByText("Manage members")).toBeTruthy();
  });

  it("renders loadingFallback while permissions load", () => {
    mockPerms({ isLoading: true });
    render(
      <PermissionGate action="manage_access" loadingFallback={<span>loading</span>}>
        <button>Manage members</button>
      </PermissionGate>,
    );
    expect(screen.getByText("loading")).toBeTruthy();
    expect(screen.queryByText("Manage members")).toBeNull();
  });
});
