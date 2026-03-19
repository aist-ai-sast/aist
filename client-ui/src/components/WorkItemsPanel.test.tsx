// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import WorkItemsPanel from "./WorkItemsPanel";

const pushToast = vi.fn();
const createMutate = vi.fn();
const deleteMutate = vi.fn();
const updateMutate = vi.fn();

let mockWorkItems: Array<{
  id: number;
  externalKey: string;
  externalUrl: string;
  title: string;
  statusCategory: "OPEN" | "IN_PROGRESS" | "DONE" | "CANCELLED" | "UNKNOWN";
  providerName: string | null;
  providerType: string | null;
  provider: number | null;
}> = [];

vi.mock("../lib/queries", () => ({
  useWorkItems: () => ({ data: mockWorkItems, isLoading: false }),
}));

vi.mock("../lib/mutations", () => ({
  useCreateWorkItem: () => ({ mutate: createMutate, isPending: false }),
  useDeleteWorkItem: () => ({ mutate: deleteMutate, isPending: false }),
  useUpdateWorkItem: () => ({ mutate: updateMutate, isPending: false }),
}));

vi.mock("./ToastProvider", () => ({
  useToast: () => ({ push: pushToast }),
}));

describe("WorkItemsPanel", () => {
  beforeEach(() => {
    mockWorkItems = [];
    vi.clearAllMocks();
  });

  it("creates a manual work item link with trimmed fields and resets the form on success", () => {
    createMutate.mockImplementation((_payload, options) => {
      options?.onSuccess?.();
    });

    render(<WorkItemsPanel findingId={42} />);

    fireEvent.click(screen.getByRole("button", { name: "+ Link issue" }));
    fireEvent.change(screen.getByPlaceholderText("Issue URL (required)"), {
      target: { value: "  https://jira.example.com/browse/SEC-42  " },
    });
    fireEvent.change(screen.getByPlaceholderText("Key (e.g. PROJ-42)"), {
      target: { value: "  SEC-42  " },
    });
    fireEvent.change(screen.getByPlaceholderText("Title (optional)"), {
      target: { value: "  Fix SQL injection  " },
    });

    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    expect(createMutate).toHaveBeenCalledWith(
      {
        external_url: "https://jira.example.com/browse/SEC-42",
        external_key: "SEC-42",
        title: "Fix SQL injection",
      },
      expect.objectContaining({
        onSuccess: expect.any(Function),
        onError: expect.any(Function),
      }),
    );
    expect(pushToast).toHaveBeenCalledWith("Work item linked.", "success");
    expect(screen.queryByPlaceholderText("Issue URL (required)")).not.toBeInTheDocument();
  });

  it("removes an existing work item link", () => {
    mockWorkItems = [
      {
        id: 7,
        externalKey: "SEC-7",
        externalUrl: "https://jira.example.com/browse/SEC-7",
        title: "Existing linked issue",
        statusCategory: "OPEN",
        providerName: null,
        providerType: null,
        provider: null,
      },
    ];
    deleteMutate.mockImplementation((_linkId, options) => {
      options?.onSuccess?.();
    });

    render(<WorkItemsPanel findingId={42} />);

    const list = screen.getByRole("list");
    expect(within(list).getByText("SEC-7")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove work item" }));

    expect(deleteMutate).toHaveBeenCalledWith(
      7,
      expect.objectContaining({
        onSuccess: expect.any(Function),
        onError: expect.any(Function),
      }),
    );
    expect(pushToast).toHaveBeenCalledWith("Work item removed.", "success");
  });
});
