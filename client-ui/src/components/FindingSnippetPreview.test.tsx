// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
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
});
