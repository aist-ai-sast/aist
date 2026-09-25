// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import FindingSnippetPreview from "./FindingSnippetPreview";
import { useFileSnippet } from "../lib/snippetCache";

vi.mock("../lib/snippetCache", () => ({
  useFileSnippet: vi.fn(),
}));

const mockedUseFileSnippet = vi.mocked(useFileSnippet);

describe("FindingSnippetPreview", () => {
  it("shows preview for line=0 with sourceFileLink", () => {
    mockedUseFileSnippet.mockReturnValue({
      snippet: {
        start: 1,
        end: 4,
        lines: ["a", "b", "c", "d"],
        highlight: null,
        fullText: "a\nb\nc\nd",
        hasHighlight: false,
      },
      isLoading: false,
      isError: false,
      isSourceUnavailable: false,
      error: null,
    } as ReturnType<typeof useFileSnippet>);

    render(<FindingSnippetPreview sourceFileLink="/file.txt" line={0} />);

    expect(screen.queryByText("Snippet preview unavailable")).toBeNull();
  });

  it("shows unavailable when sourceFileLink is missing", () => {
    render(<FindingSnippetPreview line={12} />);
    expect(screen.getByText("Snippet preview unavailable")).toBeTruthy();
  });
  it("shows the SCM credential problem instead of a generic failure", () => {
    mockedUseFileSnippet.mockReturnValue({
      snippet: null,
      isLoading: false,
      isError: true,
      isSourceUnavailable: false,
      isWarming: false,
      scmErrorMessage: "The repository rejected the organization's SCM integration token.",
      error: null,
    } as ReturnType<typeof useFileSnippet>);

    const { container } = render(<FindingSnippetPreview sourceFileLink="/settings.py" line={10} />);
    const view = within(container);

    expect(view.getByText("The repository rejected the organization's SCM integration token.")).toBeTruthy();
    expect(view.queryByText("Snippet preview unavailable")).toBeNull();
  });
});
