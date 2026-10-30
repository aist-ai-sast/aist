// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { AIResponse, Finding } from "../types";
import { EMPTY_TAG_FILTER } from "../lib/tagFilter";
import FindingDetailTabs from "./FindingDetailTabs";
import { installPopoverTestEnv } from "../lib/testPopoverEnv";

const updateFindingTags = vi.fn();

vi.mock("../lib/mutations", () => ({
  useAddFindingNote: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateFindingTags: () => ({ mutate: updateFindingTags, isPending: false }),
}));

vi.mock("../lib/queries", () => ({
  useFinding: () => ({ data: null, isLoading: false }),
  useFindingNotes: () => ({ data: [], isLoading: false }),
  useFindingTimeline: () => ({ data: [], isLoading: false }),
  useFindingTagsByProject: () => ({
    data: { names: ["backend", "pci", "next-sprint"], counts: {} },
    isLoading: false,
  }),
}));

const permissions = {
  canWrite: true,
  canOperateProject: true,
  canComment: true,
  canEnable: true,
  canManageAccess: true,
  isLoading: false,
};

vi.mock("../lib/permissions", () => ({
  usePermissions: () => permissions,
}));

vi.mock("./ToastProvider", () => ({
  useToast: () => ({ push: vi.fn() }),
}));

// CodeSnippet (rendered for the Code tab on non-DAST findings) calls
// useFileSnippet, which uses react-query's useQuery — mock it out so these
// tests don't need a QueryClientProvider.
vi.mock("../lib/snippetCache", () => ({
  useFileSnippet: vi.fn(() => ({
    snippet: null,
    isLoading: false,
    isError: false,
    isSourceUnavailable: false,
    isWarming: false,
    error: null,
  })),
}));

beforeAll(() => {
  installPopoverTestEnv();
});

beforeEach(() => {
  permissions.canWrite = true;
  updateFindingTags.mockClear();
});

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

describe("FindingDetailTabs — DAST finding view", () => {
  function buildDastFinding(overrides?: Partial<Finding>): Finding {
    return buildFinding({
      dynamicFinding: true,
      filePath: "",
      line: null,
      affectedEndpoints: ["https://api.example.com/v1/subscriptions/123"],
      param: "subscription_id",
      payload: "' OR 1=1--",
      cvssv3: "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N",
      cvssv3Score: 6.5,
      stepsToReproduce: "Authenticate as tenant A\nRequest tenant B's resource",
      references: "CWE-639\nhttps://dast-triage.internal/cp-backend/x.html",
      ...overrides,
    });
  }

  it("shows Affected Endpoints and Attack Vector blocks in Overview for a DAST finding", () => {
    render(<FindingDetailTabs finding={buildDastFinding()} />);

    expect(screen.getByText("Affected Endpoints")).toBeInTheDocument();
    expect(screen.getByText("https://api.example.com/v1/subscriptions/123")).toBeInTheDocument();

    expect(screen.getByText("Attack Vector")).toBeInTheDocument();
    expect(screen.getByText("subscription_id")).toBeInTheDocument();
    expect(screen.getByText("' OR 1=1--")).toBeInTheDocument();
    expect(screen.getByText(/CVSS:3\.1/)).toBeInTheDocument();
    expect(screen.getByText("CWE-639")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "https://dast-triage.internal/cp-backend/x.html" })).toHaveAttribute(
      "href",
      "https://dast-triage.internal/cp-backend/x.html",
    );
  });

  it("does not show DAST blocks for a normal SAST finding", () => {
    render(<FindingDetailTabs finding={buildFinding()} />);

    expect(screen.queryByText("Affected Endpoints")).not.toBeInTheDocument();
    expect(screen.queryByText("Attack Vector")).not.toBeInTheDocument();
  });

  it("renders a reference with another scheme as plain text", () => {
    render(<FindingDetailTabs finding={buildDastFinding({ references: "javascript:alert(1)" })} />);

    expect(screen.getByText("javascript:alert(1)")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "javascript:alert(1)" })).not.toBeInTheDocument();
  });

  it("relabels the Code tab to Evidence and shows structured steps to reproduce for a DAST finding", () => {
    const finding = buildDastFinding({
      stepsToReproduce:
        "1. Authenticate as tenant A\n   Request: `curl -sk https://api.example.com/v1/login`\n   Expected: HTTP 200 with a session token\n2. Request tenant B's resource\n   Request: `curl -sk https://api.example.com/v1/subscriptions/456`\n   Expected: HTTP 200 with tenant B's private data",
    });
    render(<FindingDetailTabs finding={finding} />);

    expect(screen.queryByRole("button", { name: "Code" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Evidence" }));

    expect(screen.getByText("Authenticate as tenant A")).toBeInTheDocument();
    expect(screen.getByText("Request tenant B's resource")).toBeInTheDocument();
    expect(screen.getByText("curl -sk https://api.example.com/v1/login")).toBeInTheDocument();
    expect(screen.getByText(/HTTP 200 with tenant B's private data/)).toBeInTheDocument();
  });

  it("falls back to safe prose rendering when steps_to_reproduce isn't the numbered-step convention", () => {
    render(
      <FindingDetailTabs
        finding={buildDastFinding({ stepsToReproduce: "Authenticate as tenant A, then request tenant B's resource." })}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Evidence" }));

    expect(screen.getByText(/Authenticate as tenant A/)).toBeInTheDocument();
  });

  it("shows Impact and Mitigation sections in Overview for a DAST finding", () => {
    const finding = buildDastFinding({
      impact: "- Unauthenticated attackers can read any tenant's subscription data.\n- No audit trail is left behind.",
      mitigation:
        "Scope the subscription query to the caller's own tenant. Add an authorization check before returning the resource. Log every cross-tenant access attempt.",
    });
    render(<FindingDetailTabs finding={finding} />);

    expect(screen.getByText("Impact")).toBeInTheDocument();
    expect(screen.getByText(/Unauthenticated attackers can read any tenant's subscription data/)).toBeInTheDocument();

    expect(screen.getByText("Mitigation")).toBeInTheDocument();
    // Bulletless multi-sentence mitigation should be split one sentence per bullet,
    // not shown as a single dense paragraph.
    expect(screen.getByText("Scope the subscription query to the caller's own tenant.")).toBeInTheDocument();
    expect(screen.getByText("Log every cross-tenant access attempt.")).toBeInTheDocument();
  });

  it("does not render empty Impact/Mitigation sections when the fields are absent", () => {
    render(<FindingDetailTabs finding={buildDastFinding({ impact: undefined, mitigation: undefined })} />);

    expect(screen.queryByText("Impact")).not.toBeInTheDocument();
    expect(screen.queryByText("Mitigation")).not.toBeInTheDocument();
  });

  it("keeps the Code tab and CodeSnippet for a normal SAST finding", () => {
    render(<FindingDetailTabs finding={buildFinding()} />);

    expect(screen.queryByRole("button", { name: "Evidence" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Code" }));
  });
});

describe("FindingDetailTabs — tags", () => {
  it("lets a writer add a tag and sends the whole new list once", () => {
    render(<FindingDetailTabs finding={buildFinding({ tags: ["pci"] })} permissionOrganizationId={7} />);

    fireEvent.click(screen.getByRole("button", { name: /add tag/i }));
    fireEvent.click(within(screen.getByRole("listbox")).getByRole("option", { name: /^backend/ }));
    fireEvent.click(screen.getByRole("button", { name: "Done" }));

    expect(updateFindingTags).toHaveBeenCalledTimes(1);
    expect(updateFindingTags.mock.calls[0][0]).toEqual({ id: 101, tags: ["pci", "backend"] });
  });

  it("keeps the filter menu on a chip and offers Remove tag inside it", () => {
    const onTagFilterChange = vi.fn();
    render(
      <FindingDetailTabs
        finding={buildFinding({ tags: ["pci"] })}
        permissionOrganizationId={7}
        tagFilter={EMPTY_TAG_FILTER}
        onTagFilterChange={onTagFilterChange}
      />,
    );

    // Radix DropdownMenu opens on pointerdown; keyboard opens on Enter.
    const chip = screen.getByRole("button", { name: "Filter by tag pci" });
    fireEvent.pointerDown(chip, { button: 0, ctrlKey: false, pointerType: "mouse" });
    if (!screen.queryByRole("menu")) fireEvent.keyDown(chip, { key: "Enter" });
    const menu = screen.getByRole("menu");
    expect(within(menu).getByRole("menuitem", { name: /Include tag/ })).toBeInTheDocument();

    fireEvent.click(within(menu).getByRole("menuitem", { name: /Remove tag/ }));
    expect(updateFindingTags).toHaveBeenCalledWith(
      { id: 101, tags: [] },
      expect.anything(),
    );
  });

  it("locks a tag that comes from the project and keeps it out of the saved list", () => {
    render(
      <FindingDetailTabs
        finding={buildFinding({ tags: ["pci", "ai"], projectTags: ["ai"] })}
        permissionOrganizationId={7}
      />,
    );

    expect(screen.getByTitle("From project — managed on the project card.")).toHaveTextContent("ai");

    fireEvent.click(screen.getByRole("button", { name: /Remove tag pci/ }));
    // Only the finding's own tags travel; the project's stays where it lives.
    expect(updateFindingTags).toHaveBeenCalledWith({ id: 101, tags: [] }, expect.anything());
  });

  it("still filters by an inherited tag, but drops Remove from its menu", () => {
    render(
      <FindingDetailTabs
        finding={buildFinding({ tags: ["ai"], projectTags: ["ai"] })}
        permissionOrganizationId={7}
        tagFilter={EMPTY_TAG_FILTER}
        onTagFilterChange={vi.fn()}
      />,
    );

    const chip = screen.getByRole("button", { name: "Filter by tag ai" });
    fireEvent.pointerDown(chip, { button: 0, ctrlKey: false, pointerType: "mouse" });
    if (!screen.queryByRole("menu")) fireEvent.keyDown(chip, { key: "Enter" });
    const menu = screen.getByRole("menu");

    expect(within(menu).getByRole("menuitem", { name: /Include tag/ })).toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: /Remove tag/ })).not.toBeInTheDocument();
  });

  it("shows a reader the tags without any editing control", () => {
    permissions.canWrite = false;
    render(<FindingDetailTabs finding={buildFinding({ tags: ["pci"] })} permissionOrganizationId={7} />);

    expect(screen.getByText("pci")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add tag/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Remove tag/ })).not.toBeInTheDocument();
  });
});
