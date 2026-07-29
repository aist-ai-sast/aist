// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import type { PipelineSummary } from "../types";

let mockCanOperateProject = true;
let mockPipelineItems: PipelineSummary[] = [];

vi.mock("../lib/queries", () => ({
  useProjects: () => ({ data: [] }),
  usePipelineSummaries: () => ({ data: { items: mockPipelineItems, count: mockPipelineItems.length }, isLoading: false, error: null }),
}));

vi.mock("../lib/routes", () => ({
  getRoute: () => "/findings",
}));

vi.mock("../components/PipelineFilterPanel", () => ({
  default: ({ statusOptions }: { statusOptions: Array<{ value: string; label: string }> }) => (
    <div data-testid="pipeline-filter-panel">
      {statusOptions.map((option) => (
        <span key={option.value} data-testid={`pipeline-status-option-${option.value}`}>{option.label}</span>
      ))}
    </div>
  ),
}));

vi.mock("../components/PermissionGate", () => ({
  default: ({ action, children, fallback = null }: { action: string; children: ReactNode; fallback?: ReactNode }) =>
    action === "operate_project" && mockCanOperateProject ? children : fallback,
}));

vi.mock("../components/ImportPipelineDialog", () => ({
  default: (props: { open: boolean; onClose: () => void }) => (props.open ? <div data-testid="import-dialog-open" /> : null),
  ImportUploadIcon: () => <svg data-testid="import-upload-icon" />,
}));

import PipelinesPage from "./PipelinesPage";

function renderPage() {
  return render(
    <MemoryRouter>
      <PipelinesPage />
    </MemoryRouter>,
  );
}

function pipeline(status: string, id: string): PipelineSummary {
  return {
    id,
    executionType: "SAST",
    status,
    projectId: 1,
    productId: 1,
    productName: "Status project",
    findings: 0,
    actions: [],
  };
}

describe("PipelinesPage — Import pipeline launch button", () => {
  beforeEach(() => {
    mockCanOperateProject = true;
    mockPipelineItems = [];
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it("shows the button for a user with project-operation access", () => {
    renderPage();
    expect(screen.getByRole("button", { name: /import pipeline launch/i })).toBeInTheDocument();
  });

  it("hides the button for a user without project-operation access", () => {
    mockCanOperateProject = false;
    renderPage();
    expect(screen.queryByRole("button", { name: /import pipeline launch/i })).not.toBeInTheDocument();
  });

  it("opens the import dialog when clicked", () => {
    renderPage();
    expect(screen.queryByTestId("import-dialog-open")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /import pipeline launch/i }));

    expect(screen.getByTestId("import-dialog-open")).toBeInTheDocument();
  });

  it("presents admitted and executing as distinct active lifecycle states", () => {
    mockPipelineItems = [pipeline("ADMITTED", "admitted-1"), pipeline("EXECUTING", "executing-1")];

    renderPage();

    expect(screen.getByTestId("pipeline-status-option-ADMITTED")).toHaveTextContent("Admitted");
    expect(screen.getByTestId("pipeline-status-option-EXECUTING")).toHaveTextContent("Executing");
    expect(screen.getByText("In progress: 2")).toBeInTheDocument();

    const cards = Array.from(document.querySelectorAll("article"));
    expect(within(cards[0]).getByText("Admitted")).toHaveClass("text-sky-300");
    expect(within(cards[1]).getByText("Executing")).toHaveClass("text-brand-300");
  });
});
