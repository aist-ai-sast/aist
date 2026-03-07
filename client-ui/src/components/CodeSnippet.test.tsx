// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import CodeSnippet from "./CodeSnippet";

vi.mock("@monaco-editor/react", () => ({
  default: ({ value }: { value: string }) => <pre data-testid="monaco-mock">{value}</pre>,
}));

vi.mock("../lib/snippetCache", () => ({
  useFileSnippet: vi.fn(),
}));

import { useFileSnippet } from "../lib/snippetCache";

const mockedUseFileSnippet = vi.mocked(useFileSnippet);

describe("CodeSnippet", () => {
  it("shows snippet for line=0 without highlight/jump", async () => {
    mockedUseFileSnippet.mockReturnValue({
      snippet: {
        start: 1,
        end: 3,
        lines: ["a", "b", "c"],
        highlight: null,
        fullText: "a\nb\nc",
        hasHighlight: false,
      },
      isLoading: false,
      isError: false,
      isSourceUnavailable: false,
      error: null,
    } as ReturnType<typeof useFileSnippet>);

    render(<CodeSnippet sourceFileLink="/file.txt" filePath="src/a.js" line={0} />);

    expect(screen.queryByText("Code snippet unavailable.")).toBeNull();
    expect(screen.getByText("Line is not provided by scanner; showing file content.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Jump to line" })).toBeNull();
    expect(await screen.findByTestId("monaco-mock")).toBeTruthy();
  });

  it("shows jump to line when highlight is available", () => {
    mockedUseFileSnippet.mockReturnValue({
      snippet: {
        start: 10,
        end: 16,
        lines: ["x"],
        highlight: 13,
        fullText: "x",
        hasHighlight: true,
      },
      isLoading: false,
      isError: false,
      isSourceUnavailable: false,
      error: null,
    } as ReturnType<typeof useFileSnippet>);

    render(<CodeSnippet sourceFileLink="/file.txt" filePath="src/a.js" line={13} />);

    expect(screen.getByRole("button", { name: "Jump to line" })).toBeTruthy();
  });

  it("shows unavailable when sourceFileLink is missing", () => {
    render(<CodeSnippet filePath="src/a.js" line={10} />);
    expect(screen.getByText("Code snippet unavailable.")).toBeTruthy();
  });

  it("shows specific message for 404 source file", () => {
    mockedUseFileSnippet.mockReturnValue({
      snippet: null,
      isLoading: false,
      isError: true,
      isSourceUnavailable: true,
      error: null,
    } as ReturnType<typeof useFileSnippet>);

    render(<CodeSnippet sourceFileLink="/missing.txt" filePath="src/a.js" line={10} />);
    expect(screen.getByText("Source file is unavailable for this project version.")).toBeTruthy();
  });
});
