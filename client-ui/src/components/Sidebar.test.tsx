// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import Sidebar from "./Sidebar";

let mockManageableOrgs: { id: number; name: string }[] = [];
let mockCanManageAccess = false;

vi.mock("../lib/routes", () => ({
  getRoute: (key: string) => `/${key}`,
}));

vi.mock("../lib/queries", () => ({
  useManageableOrgs: () => ({ data: mockManageableOrgs, isLoading: false }),
}));

vi.mock("../lib/permissions", () => ({
  usePermissions: () => ({
    canWrite: false,
    canComment: false,
    canEnable: false,
    canExport: true,
    canManageAccess: mockCanManageAccess,
    isLoading: false,
  }),
}));

function renderSidebar() {
  return render(
    <MemoryRouter>
      <Sidebar collapsed={false} onToggle={() => {}} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
});

describe("Sidebar — Users vs Integrations gating", () => {
  beforeEach(() => {
    mockManageableOrgs = [];
    mockCanManageAccess = false;
  });

  it("hides the Users link for a user who manages no organizations, even if canManageAccess is true", () => {
    // canManageAccess reflects DefectDojo product-type/global role — a real
    // axis, but the wrong one for org-membership management. It must not by
    // itself unlock the Users link.
    mockManageableOrgs = [];
    mockCanManageAccess = true;
    renderSidebar();
    expect(screen.queryByText("Users")).not.toBeInTheDocument();
  });

  it("shows the Users link for a user who manages at least one organization, even if canManageAccess is false", () => {
    mockManageableOrgs = [{ id: 1, name: "Acme" }];
    mockCanManageAccess = false;
    renderSidebar();
    expect(screen.getByText("Users")).toBeInTheDocument();
  });

  it("still gates Integrations by canManageAccess, independent of manageable orgs", () => {
    mockManageableOrgs = [];
    mockCanManageAccess = true;
    renderSidebar();
    expect(screen.getByText("Integrations")).toBeInTheDocument();

    cleanup();
    mockManageableOrgs = [{ id: 1, name: "Acme" }];
    mockCanManageAccess = false;
    renderSidebar();
    expect(screen.queryByText("Integrations")).not.toBeInTheDocument();
  });

  it("shows both links when both signals are satisfied", () => {
    mockManageableOrgs = [{ id: 1, name: "Acme" }];
    mockCanManageAccess = true;
    renderSidebar();
    expect(screen.getByText("Users")).toBeInTheDocument();
    expect(screen.getByText("Integrations")).toBeInTheDocument();
  });
});
