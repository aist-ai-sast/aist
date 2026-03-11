// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

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
});

