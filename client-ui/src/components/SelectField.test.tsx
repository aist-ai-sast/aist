// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import SelectField from "./SelectField";
import { installPopoverTestEnv } from "../lib/testPopoverEnv";

const PROJECTS = [
  "Checkout Web",
  "Ledger Worker",
  "Payments API",
  "Identity Gateway",
  "Notification Hub",
  "Reporting Service",
  "Partner Portal",
  "Risk Engine",
  "Settlement Core",
  "Mobile Backend",
  "Billing Adapter",
  "Fraud Scoring",
].map((name, index) => ({ value: String(index + 1), label: name }));

const STATUSES = [
  { value: "all", label: "All statuses" },
  { value: "executing", label: "Executing" },
  { value: "finished", label: "Finished" },
];

function Harness({
  options,
  searchable,
  initial = "",
  onChange,
  clearable,
}: {
  options: { value: string; label: string }[];
  searchable?: boolean;
  initial?: string;
  onChange?: (value: string) => void;
  clearable?: boolean;
}) {
  const [value, setValue] = useState(initial);
  return (
    <SelectField
      label="Project"
      placeholder="All projects"
      options={options}
      value={value}
      searchable={searchable}
      clearable={clearable}
      onChange={(next) => {
        setValue(next);
        onChange?.(next);
      }}
    />
  );
}

beforeAll(installPopoverTestEnv);
afterEach(cleanup);

describe("SelectField", () => {
  // The plain branch is a Radix Select, whose trigger carries role="combobox"; the
  // searchable branch opens from a plain button and only exposes a combobox once its
  // search box is on screen. That difference is what these two assert on.
  it("leaves a short, fixed list of statuses as a plain select", () => {
    render(<Harness options={STATUSES} />);

    expect(screen.getByRole("combobox")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Project" })).not.toBeInTheDocument();
  });

  it("searches a long project list and reports the value of the option that was picked", () => {
    const onChange = vi.fn();
    render(<Harness options={PROJECTS} onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: "Project" }));
    fireEvent.change(screen.getByPlaceholderText("Search project…"), { target: { value: "pay" } });

    expect(screen.getByRole("option", { name: "Payments API" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Ledger Worker" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("option", { name: "Payments API" }));

    // The third project in the fixture, so the raw value and not the label or an index.
    expect(onChange).toHaveBeenCalledWith("3");
    expect(screen.getByRole("button", { name: "Project" })).toHaveTextContent("Payments API");
  });

  it("says so when nothing matches instead of showing an empty list", () => {
    render(<Harness options={PROJECTS} />);

    fireEvent.click(screen.getByRole("button", { name: "Project" }));
    fireEvent.change(screen.getByPlaceholderText("Search project…"), { target: { value: "zzz" } });

    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(screen.getByText(/no match for/i)).toBeInTheDocument();
  });

  it("drops the search box when a caller turns it off on a long list", () => {
    render(<Harness options={PROJECTS} searchable={false} />);

    expect(screen.getByRole("combobox")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Project" })).not.toBeInTheDocument();
  });

  it("offers search on a short list when a caller asks for it", () => {
    render(<Harness options={STATUSES} searchable />);

    fireEvent.click(screen.getByRole("button", { name: "Project" }));

    expect(screen.getByPlaceholderText("Search project…")).toBeInTheDocument();
  });

  it("clears the selection back to an empty value through the clear row", () => {
    const onChange = vi.fn();
    render(<Harness options={PROJECTS} initial="3" clearable onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: "Project" }));
    fireEvent.click(screen.getByRole("option", { name: "None" }));

    expect(onChange).toHaveBeenCalledWith("");
  });

  it("refuses to select a disabled option", () => {
    const onChange = vi.fn();
    const options = [
      { value: "1", label: "Payments API" },
      { value: "2", label: "Archived Project", disabled: true },
    ];
    render(<Harness options={options} searchable onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: "Project" }));
    fireEvent.click(screen.getByRole("option", { name: "Archived Project" }));

    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Project" })).toHaveTextContent("All projects");
  });

  it("reopens with an empty query so a previous search does not hide the list", () => {
    render(<Harness options={PROJECTS} />);

    fireEvent.click(screen.getByRole("button", { name: "Project" }));
    fireEvent.change(screen.getByPlaceholderText("Search project…"), { target: { value: "pay" } });
    fireEvent.click(screen.getByRole("option", { name: "Payments API" }));

    fireEvent.click(screen.getByRole("button", { name: "Project" }));

    expect(screen.getByPlaceholderText("Search project…")).toHaveValue("");
    expect(screen.getByRole("option", { name: "Ledger Worker" })).toBeInTheDocument();
  });
});
