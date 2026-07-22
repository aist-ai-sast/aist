// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";

let mockCanWrite = true;

vi.mock("../lib/queries", () => ({
  useProjects: () => ({ data: [] }),
  usePipelineSummaries: () => ({ data: { items: [], count: 0 }, isLoading: false, error: null }),
}));

vi.mock("../components/PipelineFilterPanel", () => ({
  default: () => <div data-testid="pipeline-filter-panel" />,
}));

vi.mock("../components/PermissionGate", () => ({
  default: ({ action, children, fallback = null }: { action: string; children: ReactNode; fallback?: ReactNode }) =>
    action === "write" && mockCanWrite ? children : fallback,
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

describe("PipelinesPage — Import pipeline launch button", () => {
  beforeEach(() => {
    mockCanWrite = true;
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it("shows the button for a user with write access", () => {
    renderPage();
    expect(screen.getByRole("button", { name: /import pipeline launch/i })).toBeInTheDocument();
  });

  it("hides the button for a user without write access", () => {
    mockCanWrite = false;
    renderPage();
    expect(screen.queryByRole("button", { name: /import pipeline launch/i })).not.toBeInTheDocument();
  });

  it("opens the import dialog when clicked", () => {
    renderPage();
    expect(screen.queryByTestId("import-dialog-open")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /import pipeline launch/i }));

    expect(screen.getByTestId("import-dialog-open")).toBeInTheDocument();
  });
});
