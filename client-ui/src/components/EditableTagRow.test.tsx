// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import EditableTagRow from "./EditableTagRow";
import { installPopoverTestEnv } from "../lib/testPopoverEnv";

const SUGGESTIONS = ["backend", "pci", "team-payments", "aist:auto-triage"];
const ORG_ID = 7;

// The row gates itself with the shared PermissionGate, so the role is what the
// tests vary — not a boolean prop of its own.
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

/** Mirrors the real callers: the row is controlled, the parent persists. */
function Harness({
  initial = ["pci", "aist:auto-triage"],
  inherited,
  footnote,
  removalFootnote,
  onSave,
}: {
  initial?: string[];
  inherited?: string[];
  footnote?: string;
  removalFootnote?: (removed: string[]) => string;
  onSave?: (tags: string[]) => void;
}) {
  const [tags, setTags] = useState(initial);
  return (
    <EditableTagRow
      tags={tags}
      inheritedTags={inherited}
      suggestions={SUGGESTIONS}
      action="operate_project"
      permissionOrganizationId={ORG_ID}
      ownerLabel="project billing-gateway"
      footnote={footnote}
      removalFootnote={removalFootnote}
      onChange={(next) => {
        // The server keeps the tags the object does not own; mimic that here.
        const kept = tags.filter((tag) => tag.startsWith("aist:") || (inherited ?? []).includes(tag));
        setTags([...kept, ...next]);
        onSave?.(next);
      }}
    />
  );
}

function openPicker() {
  fireEvent.click(screen.getByRole("button", { name: /add tag/i }));
  return screen.getByRole("listbox");
}

function closePicker() {
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
}

beforeAll(() => {
  installPopoverTestEnv();
});

beforeEach(() => {
  permissions.canOperateProject = true;
});

afterEach(() => {
  cleanup();
});

describe("EditableTagRow", () => {
  it("adds a tag from the vocabulary and saves once, when the picker closes", () => {
    const onSave = vi.fn();
    render(<Harness onSave={onSave} />);

    const list = openPicker();
    fireEvent.click(within(list).getByRole("option", { name: /^backend/ }));
    fireEvent.click(within(list).getByRole("option", { name: /^team-payments/ }));

    // The chip appears next to the still-open picker's option, but nothing is
    // persisted yet: two "backend" nodes, the chip and the option row.
    expect(screen.getAllByText("backend")).toHaveLength(2);
    expect(onSave).not.toHaveBeenCalled();

    closePicker();
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledWith(["pci", "backend", "team-payments"]);
  });

  it("creates a tag that does not exist yet, normalising what was typed", () => {
    const onSave = vi.fn();
    render(<Harness initial={[]} onSave={onSave} />);

    openPicker();
    fireEvent.change(screen.getByPlaceholderText("Find or create a tag…"), {
      target: { value: "  Needs Design  " },
    });
    fireEvent.click(screen.getByText("needs-design"));

    closePicker();
    expect(onSave).toHaveBeenCalledWith(["needs-design"]);
  });

  it("explains why a comma or a platform prefix is refused and saves nothing", () => {
    const onSave = vi.fn();
    render(<Harness initial={[]} onSave={onSave} />);

    openPicker();
    const search = screen.getByPlaceholderText("Find or create a tag…");

    fireEvent.change(search, { target: { value: "pci, backend" } });
    expect(screen.queryByText(/^Create/)).not.toBeInTheDocument();

    fireEvent.change(search, { target: { value: "aist:mine" } });
    expect(screen.queryByText(/^Create/)).not.toBeInTheDocument();

    closePicker();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("keeps platform tags read-only", () => {
    const onSave = vi.fn();
    render(<Harness onSave={onSave} />);

    expect(screen.queryByRole("button", { name: /Remove tag aist:auto-triage/ })).not.toBeInTheDocument();

    const list = openPicker();
    fireEvent.click(within(list).getByRole("option", { name: /aist:auto-triage/ }));
    expect(screen.getByRole("alert")).toHaveTextContent("Platform tags cannot be changed.");

    closePicker();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("removes a tag straight from its chip, without opening the picker", () => {
    const onSave = vi.fn();
    render(<Harness initial={["pci", "backend"]} onSave={onSave} />);

    fireEvent.click(screen.getByRole("button", { name: /Remove tag pci/ }));
    expect(onSave).toHaveBeenCalledWith(["backend"]);
  });

  it("shows tags but no editing controls without permission", () => {
    permissions.canOperateProject = false;
    render(<Harness />);

    expect(screen.getByText("pci")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add tag/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Remove tag/ })).not.toBeInTheDocument();
  });

  it("stays read-only until the owning organization is known", () => {
    render(
      <EditableTagRow
        tags={["pci"]}
        suggestions={SUGGESTIONS}
        action="operate_project"
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByText("pci")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add tag/i })).not.toBeInTheDocument();
  });

  it("locks the controls while a write is in flight", () => {
    render(
      <EditableTagRow
        tags={["pci"]}
        suggestions={SUGGESTIONS}
        action="operate_project"
        permissionOrganizationId={ORG_ID}
        pending
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Saving…");
    expect(screen.queryByRole("button", { name: /add tag/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Remove tag/ })).not.toBeInTheDocument();
  });

  it("shows a tag inherited from the project, but offers no way to remove it", () => {
    render(<Harness initial={["pci", "ai"]} inherited={["ai"]} />);

    const chip = screen.getByTitle("From project — managed on the project card.");
    expect(chip).toHaveTextContent("ai");
    expect(screen.queryByRole("button", { name: /Remove tag ai/ })).not.toBeInTheDocument();
    // The finding's own tag is still removable.
    expect(screen.getByRole("button", { name: /Remove tag pci/ })).toBeInTheDocument();
  });

  it("explains an inherited tag in the picker instead of toggling it", () => {
    const onSave = vi.fn();
    render(<Harness initial={["pci", "ai"]} inherited={["ai"]} onSave={onSave} />);

    const list = openPicker();
    fireEvent.click(within(list).getByRole("option", { name: /^ai\b/ }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "This tag comes from the project. Edit it on the project card.",
    );

    closePicker();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("never sends an inherited tag back to the server", () => {
    const onSave = vi.fn();
    render(<Harness initial={["pci", "ai"]} inherited={["ai"]} onSave={onSave} />);

    fireEvent.click(screen.getByRole("button", { name: /Remove tag pci/ }));
    expect(onSave).toHaveBeenCalledWith([]);
  });

  it("orders the row: the object's own tags, then the project's, then the platform's", () => {
    const { container } = render(
      <Harness initial={["aist:auto-triage", "ai", "next-sprint"]} inherited={["ai"]} />,
    );

    // Every chip renders its name in a "block leading-none" span, so reading
    // those in document order reads the row left to right.
    const labels = Array.from(container.querySelectorAll("span.block")).map((node) => node.textContent);
    expect(labels).toEqual(["next-sprint", "ai", "aist:auto-triage"]);
  });

  it("says in the picker what the edit reaches, and warns louder once a tag is dropped", () => {
    render(
      <Harness
        initial={["pci", "backend"]}
        footnote="Applies to all 1240 findings in this project."
        removalFootnote={(removed) => `Removing “${removed[0]}” also removes it from 1240 findings.`}
      />,
    );

    openPicker();
    expect(screen.getByText("Applies to all 1240 findings in this project.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    fireEvent.click(within(screen.getByRole("listbox")).getByRole("option", { name: /^pci/ }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Removing “pci” also removes it from 1240 findings.",
    );
  });

  it("collapses the overflow behind a +N chip", () => {
    render(
      <EditableTagRow
        tags={["pci", "backend", "team-payments", "tier-1", "go"]}
        suggestions={SUGGESTIONS}
        action="operate_project"
        visibleCount={3}
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByText("+2")).toBeInTheDocument();
    expect(screen.queryByText("tier-1")).not.toBeInTheDocument();
  });
});
