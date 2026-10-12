// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import BulkEditBar, { type BulkEditBarProps } from "./BulkEditBar";
import { installPopoverTestEnv } from "../lib/testPopoverEnv";

function renderBar(overrides: Partial<BulkEditBarProps> = {}) {
  const props: BulkEditBarProps = {
    action: "close",
    onActionChange: vi.fn(),
    closeReason: "false_positive",
    onCloseReasonChange: vi.fn(),
    reasonNote: "",
    onReasonNoteChange: vi.fn(),
    riskAcceptEnabled: true,
    selectedCount: 12,
    visibleSelectableCount: 24,
    allVisibleSelected: false,
    onToggleSelectVisible: vi.fn(),
    onClearSelection: vi.fn(),
    onExit: vi.fn(),
    onApply: vi.fn(),
    canApply: true,
    pending: false,
    ...overrides,
  };
  render(<BulkEditBar {...props} />);
  return props;
}

/** The phone bar is the region; the desktop panel is everything outside it. */
function phoneBar() {
  return screen.getByRole("region", { name: /bulk edit/i });
}

beforeAll(installPopoverTestEnv);
afterEach(cleanup);

describe("BulkEditBar on a phone", () => {
  it("states the selection and the action it would apply without opening anything", () => {
    renderBar();

    const bar = phoneBar();
    expect(within(bar).getByText("12 selected")).toBeInTheDocument();
    expect(within(bar).getByText("Close Findings · False Positive")).toBeInTheDocument();
    expect(within(bar).getByRole("button", { name: "Apply" })).toBeEnabled();
  });

  it("refuses to apply until the reason is filled in", () => {
    renderBar({ canApply: false });

    expect(within(phoneBar()).getByRole("button", { name: "Apply" })).toBeDisabled();
  });

  it("hides the fields until the viewer asks for them", () => {
    renderBar();

    const bar = phoneBar();
    expect(within(bar).queryByPlaceholderText(/enter reason for audit log/i)).not.toBeInTheDocument();

    fireEvent.click(within(bar).getByRole("button", { name: /12 selected/ }));

    expect(within(bar).getByPlaceholderText(/enter reason for audit log/i)).toBeInTheDocument();
  });

  it("keeps clearing everything separate from deselecting what is on screen", () => {
    const props = renderBar();
    const bar = phoneBar();
    fireEvent.click(within(bar).getByRole("button", { name: /12 selected/ }));

    fireEvent.click(within(bar).getByRole("button", { name: "Select Visible (24)" }));
    expect(props.onToggleSelectVisible).toHaveBeenCalledTimes(1);
    expect(props.onClearSelection).not.toHaveBeenCalled();

    // "Clear all" counts the whole selection, which can exceed what this page shows.
    fireEvent.click(within(bar).getByRole("button", { name: "Clear all (12)" }));
    expect(props.onClearSelection).toHaveBeenCalledTimes(1);
    expect(props.onToggleSelectVisible).toHaveBeenCalledTimes(1);
  });

  it("offers to release the visible selection once all of it is selected", () => {
    renderBar({ allVisibleSelected: true });
    const bar = phoneBar();
    fireEvent.click(within(bar).getByRole("button", { name: /12 selected/ }));

    expect(within(bar).getByRole("button", { name: "Deselect Visible (24)" })).toBeInTheDocument();
  });

  it("leaves bulk edit through the close control", () => {
    const props = renderBar();

    fireEvent.click(within(phoneBar()).getByRole("button", { name: /exit bulk edit/i }));

    expect(props.onExit).toHaveBeenCalledTimes(1);
  });

  it("keeps the risk-accept warning in view, since it governs whether applying is allowed", () => {
    renderBar({ action: "risk_accept", riskAcceptEnabled: false });
    const bar = phoneBar();
    fireEvent.click(within(bar).getByRole("button", { name: /12 selected/ }));

    expect(within(bar).getByText(/full risk acceptance is not enabled/i)).toBeInTheDocument();
    expect(within(bar).getByPlaceholderText(/enter risk justification/i)).toBeInTheDocument();
  });

  it("locks every control while a bulk request is in flight", () => {
    renderBar({ pending: true, canApply: false });
    const bar = phoneBar();

    expect(within(bar).getByRole("button", { name: /exit bulk edit/i })).toBeDisabled();
    expect(within(bar).getByRole("button", { name: "Apply" })).toBeDisabled();
  });
});

describe("BulkEditBar from sm up", () => {
  it("keeps the four original actions on the in-flow panel", () => {
    renderBar();

    expect(screen.getByRole("button", { name: "Select Visible (24)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide Bulk Edit" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply to 12" })).toBeInTheDocument();
  });

  it("drops the close-reason field for an action that has no reason to pick", () => {
    renderBar({ action: "reopen" });

    expect(screen.queryByText("CLOSE AS")).not.toBeInTheDocument();
  });
});
