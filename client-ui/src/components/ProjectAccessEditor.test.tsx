// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import ProjectAccessEditor from "./ProjectAccessEditor";

const PROJECTS = [
  { id: 1, name: "payments-api" },
  { id: 2, name: "web-frontend" },
];

describe("ProjectAccessEditor", () => {
  it("grants a role when a row selector changes", () => {
    const onSetRole = vi.fn();
    render(<ProjectAccessEditor projects={PROJECTS} roleByProject={{}} onSetRole={onSetRole} />);
    const rowSelects = screen.getAllByRole("combobox");
    fireEvent.change(rowSelects[0], { target: { value: "2" } }); // Writer
    expect(onSetRole).toHaveBeenCalledWith(1, 2);
  });

  it("revokes access when a row is set to No access", () => {
    const onSetRole = vi.fn();
    render(<ProjectAccessEditor projects={PROJECTS} roleByProject={{ 1: 2 }} onSetRole={onSetRole} />);
    const rowSelects = screen.getAllByRole("combobox");
    fireEvent.change(rowSelects[0], { target: { value: "" } }); // No access
    expect(onSetRole).toHaveBeenCalledWith(1, null);
  });

  it("filters projects by search", () => {
    render(<ProjectAccessEditor projects={PROJECTS} roleByProject={{}} onSetRole={vi.fn()} />);
    fireEvent.change(screen.getByPlaceholderText("Search projects..."), { target: { value: "web" } });
    expect(screen.queryByText("payments-api")).toBeNull();
    expect(screen.getByText("web-frontend")).toBeTruthy();
  });

  it("applies a role to selected rows in bulk", () => {
    const onSetRole = vi.fn();
    render(<ProjectAccessEditor projects={PROJECTS} roleByProject={{}} onSetRole={onSetRole} />);
    const checkboxes = screen.getAllByRole("checkbox");
    // checkboxes[0] is the header select-all; select all visible rows.
    fireEvent.click(checkboxes[0]);
    // Bulk role selector is the last combobox once a selection exists.
    const combos = screen.getAllByRole("combobox");
    fireEvent.change(combos[combos.length - 1], { target: { value: "3" } }); // Maintainer
    fireEvent.click(screen.getByText("Apply to selected"));
    expect(onSetRole).toHaveBeenCalledWith(1, 3);
    expect(onSetRole).toHaveBeenCalledWith(2, 3);
  });
});
