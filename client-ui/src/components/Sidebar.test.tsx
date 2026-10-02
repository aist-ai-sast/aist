// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

import Sidebar from "./Sidebar";

let mockManageableOrgs: { id: number; name: string }[] = [];

vi.mock("../lib/routes", () => ({
  getRoute: (key: string) => `/${key}`,
}));

vi.mock("../lib/queries", () => ({
  useManageableOrgs: () => ({ data: mockManageableOrgs, isLoading: false }),
}));

function CurrentPath() {
  return <output aria-label="current path">{useLocation().pathname}</output>;
}

function renderSidebar({ collapsed = false, path = "/" }: { collapsed?: boolean; path?: string } = {}) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Sidebar collapsed={collapsed} onToggle={() => {}} />
      <Routes>
        <Route path="*" element={<CurrentPath />} />
      </Routes>
    </MemoryRouter>,
  );
}

const primaryNav = () => screen.getByRole("navigation", { name: "Primary" });
// Below `lg` the sidebar is a bottom bar; a destination is on it unless it is hidden until `lg`.
const onBottomBar = (link: HTMLElement) => !link.className.split(" ").includes("hidden");

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

describe("Sidebar — phone bottom bar", () => {
  beforeEach(() => {
    mockManageableOrgs = [{ id: 1, name: "Acme" }];
  });

  it("keeps four primary destinations plus More on the bar and moves the rest into the More sheet", () => {
    renderSidebar();

    const onBar = within(primaryNav()).getAllByRole("link").filter(onBottomBar).map((link) => link.textContent);
    expect(onBar).toEqual(["Dashboard", "Findings", "Projects", "Pipelines"]);
    expect(within(primaryNav()).getByRole("button", { name: "More" })).toBeInTheDocument();
    // My Account is reached through the profile menu on phones, never from the bar or the sheet.
    expect(onBottomBar(within(primaryNav()).getByRole("link", { name: "My Account" }))).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "More" }));
    const sheet = screen.getByRole("navigation", { name: "More" });
    expect(within(sheet).getAllByRole("link").map((link) => link.textContent)).toEqual([
      "Calendar",
      "Users",
      "Integrations",
    ]);
    expect(within(sheet).queryByRole("link", { name: "My Account" })).not.toBeInTheDocument();
  });

  it("navigates from the More sheet and closes it", () => {
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: "More" }));
    fireEvent.click(within(screen.getByRole("navigation", { name: "More" })).getByRole("link", { name: "Integrations" }));

    expect(screen.getByRole("status", { name: "current path" })).toHaveTextContent("/ui_org_integrations_path");
    expect(screen.queryByRole("navigation", { name: "More" })).not.toBeInTheDocument();
  });

  it("highlights More while the current page is one of its destinations", () => {
    renderSidebar({ path: "/ui_org_integrations_path" });

    expect(screen.getByRole("button", { name: "More" })).toHaveClass("text-white");
  });

  it("does not highlight More on a primary destination", () => {
    renderSidebar({ path: "/ui_dashboard_path" });

    expect(screen.getByRole("button", { name: "More" })).not.toHaveClass("text-white");
  });

  it("puts a lone secondary destination on the bar instead of a one-item sheet", () => {
    mockManageableOrgs = [];
    renderSidebar();

    expect(screen.queryByRole("button", { name: "More" })).not.toBeInTheDocument();
    expect(onBottomBar(within(primaryNav()).getByRole("link", { name: "Calendar" }))).toBe(true);
  });

  it("gives every destination an accessible name, also in the collapsed desktop sidebar", () => {
    renderSidebar({ collapsed: true });

    for (const name of ["Dashboard", "Findings", "Projects", "Pipelines", "Calendar", "Users", "Integrations", "My Account"]) {
      expect(within(primaryNav()).getByRole("link", { name })).toBeInTheDocument();
    }
  });
});
