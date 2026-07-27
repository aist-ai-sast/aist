// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ImportPipelineDialog from "./ImportPipelineDialog";

const pushToast = vi.fn();
const validateMutateAsync = vi.fn();
const importMutateAsync = vi.fn();
let validatePending = false;
let importPending = false;
let pipelineStatus: { status: string; run_task_id: string | null } | undefined;
let projectBindings = [
  {
    id: 11,
    project: 1,
    target: { display_name: "Cloud app" },
    source_repo_key: "backend",
    enabled: true,
  },
];

vi.mock("../lib/queries", () => ({
  useProjects: () => ({
    data: [
      { id: 1, productId: 1, name: "cloud_portal", organizationId: 1, organizationName: "Acme" },
      { id: 2, productId: 2, name: "other-project", organizationId: 1, organizationName: "Acme" },
    ],
  }),
  useProjectDastBindings: () => ({ data: projectBindings, isLoading: false }),
  usePipelineStatus: () => ({ data: pipelineStatus }),
}));

vi.mock("../lib/mutations", () => ({
  useValidateImportPipeline: () => ({ mutateAsync: validateMutateAsync, isPending: validatePending }),
  useImportPipeline: () => ({ mutateAsync: importMutateAsync, isPending: importPending }),
}));

vi.mock("./ToastProvider", () => ({
  useToast: () => ({ push: pushToast }),
}));

function reportFile(name = "generic-aist-report.json") {
  return new File(["irrelevant — the backend parses this, not the browser"], name, { type: "application/json" });
}

// The project field is the app's Radix-based SelectField — button/listbox-driven:
// open it with a click, then click the option (see ProjectAccessEditor.test.tsx).
async function selectProjectAndUploadFile(name = "generic-aist-report.json") {
  fireEvent.click(screen.getAllByRole("combobox")[0]);
  fireEvent.click(screen.getByRole("option", { name: "cloud_portal" }));
  fireEvent.click(screen.getAllByRole("combobox")[1]);
  fireEvent.click(screen.getByRole("option", { name: "Cloud app · backend" }));
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  const file = reportFile(name);
  fireEvent.change(input, { target: { files: [file] } });
  await waitFor(() => expect(screen.getByText(name)).toBeInTheDocument());
}

describe("ImportPipelineDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    validatePending = false;
    importPending = false;
    pipelineStatus = undefined;
    projectBindings = [
      {
        id: 11,
        project: 1,
        target: { display_name: "Cloud app" },
        source_repo_key: "backend",
        enabled: true,
      },
    ];
  });

  afterEach(() => {
    cleanup();
  });

  it("never reads the file itself — hands it straight to the backend validate endpoint", async () => {
    validateMutateAsync.mockResolvedValue({
      findings_count: 3,
      severity_breakdown: { High: 1, Medium: 1, Low: 1 },
      name: "DAST",
      version: "backend@fd5b25aa1234",
      actual_source_commit: "fd5b25aa1234567890abcdef1234567890abcdef",
    });
    render(<ImportPipelineDialog open onClose={vi.fn()} />);

    await selectProjectAndUploadFile();

    expect(validateMutateAsync).toHaveBeenCalledWith(
      expect.objectContaining({ projectId: 1, bindingId: 11, scanType: "DAST Autonomous Scan" }),
    );
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("High: 1")).toBeInTheDocument();
    expect(screen.getByText(/DAST backend@fd5b25aa1234/)).toBeInTheDocument();
  });

  it("shows the authoritative source commit read-only and enables Create pipeline", async () => {
    validateMutateAsync.mockResolvedValue({
      findings_count: 1,
      severity_breakdown: { High: 1 },
      name: "DAST",
      version: "backend@fd5b25aa1234",
      actual_source_commit: "fd5b25aa1234567890abcdef1234567890abcdef",
    });
    render(<ImportPipelineDialog open onClose={vi.fn()} />);

    await selectProjectAndUploadFile();

    expect(screen.getByText("fd5b25aa1234567890abcdef1234567890abcdef")).toBeInTheDocument();
    expect(screen.getByText(/cannot be overridden/i)).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /create pipeline/i })).not.toBeDisabled();
  });

  it("disables Create pipeline when the validated report has no authoritative source commit", async () => {
    validateMutateAsync.mockResolvedValue({
      findings_count: 1,
      severity_breakdown: { High: 1 },
      name: "DAST",
      version: "backend@fd5b25aa1234",
      actual_source_commit: null,
    });
    render(<ImportPipelineDialog open onClose={vi.fn()} />);

    await selectProjectAndUploadFile();

    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /create pipeline/i })).toBeDisabled();
  });

  it("shows the backend's parser error when validation fails", async () => {
    validateMutateAsync.mockRejectedValue(new Error("type must be 'DAST Autonomous Scan', got 'Generic Findings Import'."));
    render(<ImportPipelineDialog open onClose={vi.fn()} />);

    await selectProjectAndUploadFile();

    expect(screen.getByText(/report can't be imported/i)).toBeInTheDocument();
    expect(screen.getByText(/type must be 'DAST Autonomous Scan'/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /create pipeline/i })).not.toBeInTheDocument();
  });

  it("submits the same File object and explicit binding without a commit override", async () => {
    validateMutateAsync.mockResolvedValue({
      findings_count: 1,
      severity_breakdown: { High: 1 },
      name: "DAST",
      version: "backend@fd5b25aa1234",
      actual_source_commit: "fd5b25aa1234567890abcdef1234567890abcdef",
    });
    importMutateAsync.mockResolvedValue({ pipeline_id: "abcd1234", run_task_id: "task-1" });
    render(<ImportPipelineDialog open onClose={vi.fn()} />);

    await selectProjectAndUploadFile();

    fireEvent.click(screen.getByRole("button", { name: /create pipeline/i }));

    await waitFor(() => expect(importMutateAsync).toHaveBeenCalled());
    const call = importMutateAsync.mock.calls[0][0];
    expect(call.projectId).toBe(1);
    expect(call.bindingId).toBe(11);
    expect(call.scanType).toBe("DAST Autonomous Scan");
    expect(call).not.toHaveProperty("commitHash");
    expect(call.file).toBeInstanceOf(File);
  });

  it("validates a dropped file via the drop event, instead of letting the browser navigate to it", async () => {
    validateMutateAsync.mockResolvedValue({
      findings_count: 2,
      severity_breakdown: { High: 2 },
      name: "DAST",
      version: "backend@fd5b25aa1234",
      actual_source_commit: null,
    });
    render(<ImportPipelineDialog open onClose={vi.fn()} />);

    fireEvent.click(screen.getAllByRole("combobox")[0]);
    fireEvent.click(screen.getByRole("option", { name: "cloud_portal" }));
    fireEvent.click(screen.getAllByRole("combobox")[1]);
    fireEvent.click(screen.getByRole("option", { name: "Cloud app · backend" }));

    const dropZone = screen.getByRole("button", { name: /drop a report file here/i });
    const file = reportFile("dropped-report.json");

    const dropEvent = new Event("drop", { bubbles: true, cancelable: true });
    Object.defineProperty(dropEvent, "dataTransfer", { value: { files: [file], types: ["Files"] } });
    fireEvent(dropZone, dropEvent);

    expect(dropEvent.defaultPrevented).toBe(true);
    await waitFor(() => expect(validateMutateAsync).toHaveBeenCalledWith(
      expect.objectContaining({ projectId: 1, bindingId: 11, scanType: "DAST Autonomous Scan" }),
    ));
    await waitFor(() => expect(screen.getByText("dropped-report.json")).toBeInTheDocument());
  });

  it("prevents the browser's default navigation on dragover even before a project is selected", () => {
    render(<ImportPipelineDialog open onClose={vi.fn()} />);

    const dropZone = screen.getByRole("button", { name: /drop a report file here/i });
    const dragOverEvent = new Event("dragover", { bubbles: true, cancelable: true });
    Object.defineProperty(dragOverEvent, "dataTransfer", { value: { files: [], types: ["Files"], items: [] } });
    fireEvent(dropZone, dragOverEvent);

    expect(dragOverEvent.defaultPrevented).toBe(true);
  });

  it("opens the file picker on click once a project and binding are selected", () => {
    render(<ImportPipelineDialog open onClose={vi.fn()} />);

    fireEvent.click(screen.getAllByRole("combobox")[0]);
    fireEvent.click(screen.getByRole("option", { name: "cloud_portal" }));
    fireEvent.click(screen.getAllByRole("combobox")[1]);
    fireEvent.click(screen.getByRole("option", { name: "Cloud app · backend" }));

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const clickSpy = vi.spyOn(input, "click");

    fireEvent.click(screen.getByRole("button", { name: /drop a report file here/i }));

    expect(clickSpy).toHaveBeenCalledOnce();
  });

  it("does not open the file picker on click before a project is selected", () => {
    render(<ImportPipelineDialog open onClose={vi.fn()} />);

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const clickSpy = vi.spyOn(input, "click");

    fireEvent.click(screen.getByRole("button", { name: /drop a report file here/i }));

    expect(clickSpy).not.toHaveBeenCalled();
  });

  it("keeps upload disabled when the project has no enabled DAST binding", () => {
    projectBindings = [];
    render(<ImportPipelineDialog open onClose={vi.fn()} />);

    fireEvent.click(screen.getAllByRole("combobox")[0]);
    fireEvent.click(screen.getByRole("option", { name: "cloud_portal" }));

    expect(screen.getByText(/no enabled DAST target binding/i)).toBeInTheDocument();
    expect(screen.getAllByRole("combobox")[1]).toBeDisabled();
    expect(screen.getByRole("button", { name: /drop a report file here/i })).toHaveAttribute("aria-disabled", "true");
  });

  it("waits for run_task_id to clear before treating FINISHED as terminal", async () => {
    validateMutateAsync.mockResolvedValue({
      findings_count: 1,
      severity_breakdown: { High: 1 },
      name: "DAST",
      version: "backend@fd5b25aa1234",
      actual_source_commit: "fd5b25aa1234567890abcdef1234567890abcdef",
    });
    importMutateAsync.mockResolvedValue({ pipeline_id: "abcd1234", run_task_id: "task-1" });
    const onClose = vi.fn();
    const view = render(<ImportPipelineDialog open onClose={onClose} />);

    await selectProjectAndUploadFile();
    fireEvent.click(screen.getByRole("button", { name: /create pipeline/i }));
    await waitFor(() => expect(screen.getByText(/importing report/i)).toBeInTheDocument());

    pipelineStatus = { status: "FINISHED", run_task_id: "task-1" };
    view.rerender(<ImportPipelineDialog open onClose={onClose} />);
    expect(onClose).not.toHaveBeenCalled();

    pipelineStatus = { status: "FINISHED", run_task_id: null };
    view.rerender(<ImportPipelineDialog open onClose={onClose} />);
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
  });
});
