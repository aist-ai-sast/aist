// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import Sidebar from "./Sidebar";

let mockManageableOrgs: { id: number; name: string }[] = [];

vi.mock("../lib/routes", () => ({
  getRoute: (key: string) => `/${key}`,
}));

vi.mock("../lib/queries", () => ({
  useManageableOrgs: () => ({ data: mockManageableOrgs, isLoading: false }),
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

describe("Sidebar — tenant-aware management gating", () => {
  beforeEach(() => {
    mockManageableOrgs = [];
  });

  it("hides both management links when no organization is manageable", () => {
    mockManageableOrgs = [];
    renderSidebar();
    expect(screen.queryByText("Users")).not.toBeInTheDocument();
    expect(screen.queryByText("Integrations")).not.toBeInTheDocument();
  });

  it("shows both management links when at least one organization is manageable", () => {
    mockManageableOrgs = [{ id: 1, name: "Acme" }];
    renderSidebar();
    expect(screen.getByText("Users")).toBeInTheDocument();
    expect(screen.getByText("Integrations")).toBeInTheDocument();
  });
});
