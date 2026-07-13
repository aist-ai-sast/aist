// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import ProjectAccessEditor from "./ProjectAccessEditor";

const PROJECTS = [
  { id: 1, name: "payments-api" },
  { id: 2, name: "web-frontend" },
];

afterEach(() => {
  cleanup();
});

// ProjectAccessEditor now uses the app's Radix-based SelectField instead of a
// native <select> (Task 13) — its trigger has role "combobox" but is
// button/listbox-driven: open it with a click, then click the option.
function chooseOption(trigger: HTMLElement, optionName: string) {
  fireEvent.click(trigger);
  fireEvent.click(screen.getByRole("option", { name: optionName }));
}

describe("ProjectAccessEditor", () => {
  it("grants a role when a row selector changes", () => {
    const onSetRole = vi.fn();
    render(<ProjectAccessEditor projects={PROJECTS} roleByProject={{}} onSetRole={onSetRole} />);
    const rowSelects = screen.getAllByRole("combobox");
    chooseOption(rowSelects[0], "Writer");
    expect(onSetRole).toHaveBeenCalledWith(1, 2);
  });

  it("revokes access when a row is set to No access", () => {
    const onSetRole = vi.fn();
    render(<ProjectAccessEditor projects={PROJECTS} roleByProject={{ 1: 2 }} onSetRole={onSetRole} />);
    const rowSelects = screen.getAllByRole("combobox");
    chooseOption(rowSelects[0], "No access");
    expect(onSetRole).toHaveBeenCalledWith(1, null);
  });

  it("filters projects by search", () => {
    render(<ProjectAccessEditor projects={PROJECTS} roleByProject={{}} onSetRole={vi.fn()} />);
    fireEvent.change(screen.getByPlaceholderText("Search projects..."), { target: { value: "web" } });
    expect(screen.queryByText("payments-api")).toBeNull();
    expect(screen.getByText("web-frontend")).toBeTruthy();
  });

  it("applies a role to selected rows in bulk", async () => {
    const onSetRole = vi.fn();
    render(<ProjectAccessEditor projects={PROJECTS} roleByProject={{}} onSetRole={onSetRole} />);
    const checkboxes = screen.getAllByRole("checkbox");
    // checkboxes[0] is the header select-all; select all visible rows.
    fireEvent.click(checkboxes[0]);
    // Bulk role selector is the last combobox once a selection exists.
    const combos = screen.getAllByRole("combobox");
    chooseOption(combos[combos.length - 1], "Maintainer");
    fireEvent.click(screen.getByText("Apply to selected"));
    await waitFor(() => expect(onSetRole).toHaveBeenCalledWith(1, 3));
    await waitFor(() => expect(onSetRole).toHaveBeenCalledWith(2, 3));
  });

  it("shows the current role as the selected label, not just a placeholder", () => {
    render(<ProjectAccessEditor projects={PROJECTS} roleByProject={{ 1: 4 }} onSetRole={vi.fn()} />);
    const [paymentsRow] = screen.getAllByRole("combobox");
    expect(paymentsRow.textContent).toContain("Owner");
  });

  it("applies bulk changes one at a time, not as an unbounded fan-out of concurrent calls", async () => {
    // Regression test: "select all" + bulk apply used to fire one onSetRole
    // call per row synchronously via Array.forEach, with no concurrency limit
    // and no way to block further input before the first request even
    // resolved. It must now await each call before starting the next.
    const inFlight: number[] = [];
    const completed: number[] = [];
    let releaseFirst!: () => void;
    const onSetRole = vi.fn((projectId: number) => {
      inFlight.push(projectId);
      if (inFlight.length === 1) {
        return new Promise<void>((resolve) => {
          releaseFirst = () => {
            completed.push(projectId);
            resolve();
          };
        });
      }
      completed.push(projectId);
      return Promise.resolve();
    });

    render(<ProjectAccessEditor projects={PROJECTS} roleByProject={{}} onSetRole={onSetRole} />);
    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    const combos = screen.getAllByRole("combobox");
    chooseOption(combos[combos.length - 1], "Maintainer");
    fireEvent.click(screen.getByText("Apply to selected"));

    // Only the first call has fired; the second must wait for it. Row
    // controls are disabled immediately, before the first promise resolves.
    expect(inFlight).toEqual([1]);
    expect(screen.getAllByRole("checkbox").every((el) => (el as HTMLInputElement).disabled)).toBe(true);

    releaseFirst();
    await waitFor(() => expect(completed).toEqual([1, 2]));
    await waitFor(() => expect(screen.getAllByRole("checkbox").every((el) => (el as HTMLInputElement).disabled)).toBe(false));
  });

  it("greys out (but still shows) roles above maxRoleId instead of hiding them", () => {
    // Regression: a full member's per-project role can only downgrade their
    // org-wide role — the backend rejects anything higher with a 400
    // (service.py's _grant_project). The dropdown used to offer every role
    // unconditionally, so picking one above the member's org role looked
    // valid right up until the 400 came back.
    const onSetRole = vi.fn();
    // Writer (id 2) is the member's org role: Reader/Writer stay pickable,
    // Maintainer/Owner are shown but disabled.
    render(
      <ProjectAccessEditor projects={PROJECTS} roleByProject={{}} onSetRole={onSetRole} maxRoleId={2} />,
    );
    const [paymentsRow] = screen.getAllByRole("combobox");
    fireEvent.click(paymentsRow);

    expect(screen.getByRole("option", { name: "Reader" }).getAttribute("aria-disabled")).not.toBe("true");
    expect(screen.getByRole("option", { name: "Writer" }).getAttribute("aria-disabled")).not.toBe("true");
    expect(screen.getByRole("option", { name: "Maintainer" }).getAttribute("aria-disabled")).toBe("true");
    expect(screen.getByRole("option", { name: "Owner" }).getAttribute("aria-disabled")).toBe("true");

    fireEvent.click(screen.getByRole("option", { name: "Maintainer" }));
    expect(onSetRole).not.toHaveBeenCalled();
  });

  it("offers every role unrestricted when maxRoleId is not set (restricted members, invite flow)", () => {
    const onSetRole = vi.fn();
    render(<ProjectAccessEditor projects={PROJECTS} roleByProject={{}} onSetRole={onSetRole} />);
    const [paymentsRow] = screen.getAllByRole("combobox");
    chooseOption(paymentsRow, "Owner");
    expect(onSetRole).toHaveBeenCalledWith(1, 4);
  });

  it("renders a malicious project name as literal text, not markup", () => {
    const xssName = "<script>alert(1)</script>";
    render(
      <ProjectAccessEditor
        projects={[{ id: 1, name: xssName }]}
        roleByProject={{}}
        onSetRole={vi.fn()}
      />,
    );

    const nameNode = screen.getByText(xssName);
    expect(nameNode.textContent).toBe(xssName);
    expect(document.querySelector("script")).toBeNull();
    expect(document.body.innerHTML).not.toContain("<script>alert(1)</script>");
  });
});
