// @vitest-environment jsdom
import { fireEvent, render, screen, act } from "@testing-library/react";
import type { ChangeEvent, ReactNode } from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";

import type { Finding } from "../types";
import type { RiskApprovalStatus } from "../lib/queries";
import FindingStatusActions from "./FindingStatusActions";

const closeMutate = vi.fn();
const riskMutate = vi.fn();
const reopenMutate = vi.fn();
const revokeMutate = vi.fn();

let mockRiskApprovalStatus: RiskApprovalStatus | null = { enabled: true, current: null };

vi.mock("../lib/mutations", () => ({
  useCloseFinding: () => ({ mutate: closeMutate, isPending: false }),
  useRiskApproveFinding: () => ({ mutate: riskMutate, isPending: false }),
  useUpdateFindingStatus: () => ({ mutate: reopenMutate, isPending: false }),
  useRevokeRiskApproval: () => ({ mutate: revokeMutate, isPending: false }),
}));

vi.mock("../lib/queries", () => ({
  useRiskApprovalStatus: () => ({ data: mockRiskApprovalStatus, isLoading: false }),
}));

vi.mock("./PermissionGate", () => ({
  default: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

vi.mock("./ToastProvider", () => ({
  useToast: () => ({ push: vi.fn() }),
}));

vi.mock("../lib/dateDisplay", () => ({
  formatDateForUI: (s: string) => s,
}));

vi.mock("./DateField", () => ({
  default: ({
    label,
    value,
    onChange,
  }: {
    label: string;
    value: string;
    onChange: (value: string) => void;
  }) => (
    <label>
      {label}
      <input
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  ),
}));

vi.mock("./TextInput", () => ({
  default: ({
    value,
    onChange,
    placeholder,
  }: {
    value: string;
    onChange: (event: ChangeEvent<HTMLInputElement>) => void;
    placeholder?: string;
  }) => (
    <input
      aria-label={placeholder ?? "text-input"}
      value={value}
      onChange={onChange}
      placeholder={placeholder}
    />
  ),
}));

vi.mock("./SelectField", () => ({
  default: ({
    label,
    value,
    onChange,
    options,
  }: {
    label: string;
    value: string;
    onChange: (value: string) => void;
    options: { value: string; label: string }[];
  }) => (
    <label>
      {label}
      <select
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  ),
}));

function buildFinding(overrides?: Partial<Finding>): Finding {
  return {
    id: 10,
    title: "Credential leak in config",
    severity: "High",
    active: true,
    product: "1",
    filePath: "src/main.py",
    line: 12,
    tool: "semgrep",
    ...overrides,
  };
}

describe("FindingStatusActions", () => {
  beforeEach(() => {
    closeMutate.mockReset();
    riskMutate.mockReset();
    reopenMutate.mockReset();
    revokeMutate.mockReset();
    mockRiskApprovalStatus = { enabled: true, current: null };
  });

  it("applies regular close action by default", () => {
    render(<FindingStatusActions finding={buildFinding()} />);

    fireEvent.click(screen.getByRole("button", { name: "Apply Close" }));

    expect(closeMutate).toHaveBeenCalledTimes(1);
    expect(riskMutate).not.toHaveBeenCalled();
  });

  it("opens risk approval modal and calls risk approval mutation", () => {
    render(<FindingStatusActions finding={buildFinding()} />);

    fireEvent.click(screen.getByRole("button", { name: "Risk Approval" }));
    fireEvent.change(screen.getByPlaceholderText("Explain why this risk is accepted and what controls exist."), {
      target: { value: "Risk accepted with compensating controls." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm Risk Approval" }));

    expect(riskMutate).toHaveBeenCalledTimes(1);
    expect(closeMutate).not.toHaveBeenCalled();
    expect(riskMutate.mock.calls[0][0]).toMatchObject({
      id: 10,
      justification: "Risk accepted with compensating controls.",
      reactivateExpired: true,
    });
  });

  it("resets modal fields when reopened after partial fill", () => {
    render(<FindingStatusActions finding={buildFinding()} />);

    // Open, fill, close
    fireEvent.click(screen.getByRole("button", { name: "Risk Approval" }));
    fireEvent.change(screen.getByPlaceholderText("Explain why this risk is accepted and what controls exist."), {
      target: { value: "some leftover text" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    // Reopen — fields should be empty again
    fireEvent.click(screen.getByRole("button", { name: "Risk Approval" }));
    expect(screen.getByPlaceholderText("Explain why this risk is accepted and what controls exist.")).toHaveValue("");
  });

  it("does not call mutation when justification is empty", () => {
    render(<FindingStatusActions finding={buildFinding()} />);

    fireEvent.click(screen.getByRole("button", { name: "Risk Approval" }));
    const confirmButton = screen.getByRole("button", { name: "Confirm Risk Approval" });
    expect(confirmButton).toBeDisabled();
    expect(riskMutate).not.toHaveBeenCalled();
  });

  it("modal header shows finding title and severity", () => {
    render(<FindingStatusActions finding={buildFinding()} />);

    fireEvent.click(screen.getByRole("button", { name: "Risk Approval" }));

    expect(screen.getByTitle("Credential leak in config")).toBeTruthy();
    expect(screen.getByText("High")).toBeTruthy();
  });

  it("shows character counter in modal", () => {
    render(<FindingStatusActions finding={buildFinding()} />);

    fireEvent.click(screen.getByRole("button", { name: "Risk Approval" }));
    expect(screen.getByText("0/4096")).toBeTruthy();

    fireEvent.change(screen.getByPlaceholderText("Explain why this risk is accepted and what controls exist."), {
      target: { value: "abc" },
    });
    expect(screen.getByText("3/4096")).toBeTruthy();
  });

  it("closes modal on Escape key press", () => {
    render(<FindingStatusActions finding={buildFinding()} />);

    fireEvent.click(screen.getByRole("button", { name: "Risk Approval" }));
    expect(screen.getByRole("dialog")).toBeTruthy();

    act(() => {
      fireEvent.keyDown(document, { key: "Escape" });
    });

    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("disables Risk Approval button when product has feature disabled", () => {
    mockRiskApprovalStatus = { enabled: false, current: null };
    render(<FindingStatusActions finding={buildFinding()} />);

    const button = screen.getByRole("button", { name: "Risk Approval" });
    expect(button).toBeDisabled();
  });

  it("shows reopen button for inactive non-risk-accepted finding", () => {
    render(<FindingStatusActions finding={buildFinding({ active: false, riskAccepted: false })} />);

    expect(screen.queryByRole("button", { name: "Apply Close" })).toBeNull();
    expect(screen.getByRole("button", { name: "Reopen" })).toBeTruthy();
  });

  it("shows risk accepted banner with revoke for inactive risk-accepted finding", () => {
    mockRiskApprovalStatus = {
      enabled: true,
      current: {
        id: 5,
        acceptedBy: "Security Manager",
        expirationDate: "2027-06-01",
        reactivateExpired: true,
        decisionDetails: "Accepted due to business priority.",
        created: "2026-01-01T00:00:00Z",
      },
    };
    render(<FindingStatusActions finding={buildFinding({ active: false, riskAccepted: true })} />);

    expect(screen.queryByRole("button", { name: "Reopen" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Apply Close" })).toBeNull();
    expect(screen.getByText("Risk Accepted")).toBeTruthy();
    // Banner shows acceptedBy inline (no "Accepted by:" label)
    expect(screen.getByText("Security Manager")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Revoke" })).toBeTruthy();
  });

  it("shows risk accepted banner immediately after approval before finding prop updates", () => {
    // Simulate scenario: finding is still active in props (optimistic update lag),
    // but approval mutation just succeeded — localRiskApproved should show banner.
    // We simulate this by triggering the modal success path via riskMutate callback.
    const onApplied = vi.fn();
    render(<FindingStatusActions finding={buildFinding({ active: true })} onApplied={onApplied} />);

    fireEvent.click(screen.getByRole("button", { name: "Risk Approval" }));
    fireEvent.change(screen.getByPlaceholderText("Explain why this risk is accepted and what controls exist."), {
      target: { value: "Business decision." },
    });

    // Simulate mutation calling onSuccess with onApplied
    riskMutate.mockImplementation((_vars: unknown, opts: { onSuccess?: () => void }) => {
      opts?.onSuccess?.();
    });

    fireEvent.click(screen.getByRole("button", { name: "Confirm Risk Approval" }));
    expect(onApplied).toHaveBeenCalledWith("risk_accepted");
  });

  it("calls revoke mutation when Revoke is clicked", () => {
    mockRiskApprovalStatus = {
      enabled: true,
      current: {
        id: 5,
        acceptedBy: "Owner",
        expirationDate: null,
        reactivateExpired: true,
        decisionDetails: "Reason.",
        created: "2026-01-01T00:00:00Z",
      },
    };
    render(<FindingStatusActions finding={buildFinding({ active: false, riskAccepted: true })} />);

    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));

    expect(revokeMutate).toHaveBeenCalledTimes(1);
    expect(revokeMutate.mock.calls[0][0]).toMatchObject({ id: 10 });
  });
});
