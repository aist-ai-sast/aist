// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AIResponse, Finding } from "../types";
import FindingDetailTabs from "./FindingDetailTabs";

vi.mock("../lib/mutations", () => ({
  useAddFindingNote: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("../lib/queries", () => ({
  useFinding: () => ({ data: null, isLoading: false }),
  useFindingNotes: () => ({ data: [], isLoading: false }),
  useFindingTimeline: () => ({ data: [], isLoading: false }),
}));

vi.mock("./PermissionGate", () => ({
  default: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

vi.mock("./ToastProvider", () => ({
  useToast: () => ({ push: vi.fn() }),
}));

afterEach(() => {
  cleanup();
});

function buildFinding(overrides?: Partial<Finding>): Finding {
  return {
    id: 101,
    title: "Test finding",
    severity: "High",
    active: true,
    product: "Demo",
    filePath: "src/app.py",
    line: 15,
    tool: "tool",
    description: "Plain description",
    ...overrides,
  };
}

describe("FindingDetailTabs", () => {
  it("renders AI reasoning as markdown in AI Assessment tab", () => {
    const aiResponse: AIResponse = {
      verdict: "true_positive",
      title: "Sample AI title",
      reasoning: "## Evidence\n- User input reaches sink\n- Missing output encoding",
      references: [],
    };

    render(<FindingDetailTabs finding={buildFinding()} aiResponse={aiResponse} />);
    fireEvent.click(screen.getByRole("button", { name: "AI Assessment" }));

    expect(
      screen.getByRole("heading", { level: 2, name: "Evidence" }),
    ).toBeInTheDocument();
    expect(screen.getByText("User input reaches sink")).toBeInTheDocument();
    expect(screen.getByText("Missing output encoding")).toBeInTheDocument();
  });

  it("shows fix panel in AI Assessment tab when fix is present", () => {
    const aiResponse: AIResponse = {
      verdict: "true_positive",
      reasoning: "Container runs as root.",
      fix: {
        fixSummary: "Add USER instruction to run as non-root.",
        fixType: "config_change",
        diffAvailable: true,
        stepByStep: ["Step 1: Add USER appuser"],
        diff: "--- a/Dockerfile\n+++ b/Dockerfile\n+USER appuser",
        codeAfter: null,
        testingHint: null,
        secretsManagement: null,
        suppressionAnnotation: null,
      },
    };

    render(<FindingDetailTabs finding={buildFinding()} aiResponse={aiResponse} />);
    fireEvent.click(screen.getByRole("button", { name: "AI Assessment" }));

    expect(screen.getByText("Add USER instruction to run as non-root.")).toBeInTheDocument();
    expect(screen.getByText("Config Change")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /show fix/i })).toBeInTheDocument();
  });

  it("does not show fix panel when fix is absent", () => {
    const aiResponse: AIResponse = {
      verdict: "uncertain",
      reasoning: "Could not determine.",
    };

    render(<FindingDetailTabs finding={buildFinding()} aiResponse={aiResponse} />);
    fireEvent.click(screen.getByRole("button", { name: "AI Assessment" }));

    expect(screen.queryByText("Fix available")).not.toBeInTheDocument();
  });
});
