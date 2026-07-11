// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, fetchFileContent, SourceWarmingError } from "./api";
import { useFileSnippet } from "./snippetCache";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    fetchFileContent: vi.fn(),
  };
});

const mockedFetchFileContent = vi.mocked(fetchFileContent);

function withQueryClient(children: ReactNode) {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("useFileSnippet", () => {
  it("returns snippet for line=0 without highlight", async () => {
    mockedFetchFileContent.mockResolvedValueOnce("a\nb\nc\nd");
    const { result } = renderHook(
      () =>
        useFileSnippet({
          sourceFileLink: "/file.txt",
          line: 0,
        }),
      { wrapper: ({ children }) => withQueryClient(children) },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.snippet).not.toBeNull();
    expect(result.current.snippet?.highlight).toBeNull();
    expect(result.current.snippet?.lines.join("\n")).toContain("a");
    expect(result.current.isSourceUnavailable).toBe(false);
  });

  it("marks source unavailable on 404", async () => {
    mockedFetchFileContent.mockRejectedValue(
      new ApiError({
        status: 404,
        code: "http_error",
        payload: null,
        url: "/missing.txt",
      }),
    );
    const { result } = renderHook(
      () =>
        useFileSnippet({
          sourceFileLink: "/missing.txt",
          line: 10,
        }),
      { wrapper: ({ children }) => withQueryClient(children) },
    );

    await waitFor(() => expect(result.current.isError).toBe(true), { timeout: 3000 });
    expect(result.current.isSourceUnavailable).toBe(true);
  });

  it("reports isWarming while the VPN egress tunnel warms up (202)", async () => {
    // retryAfter is tiny so the bounded retries elapse quickly in the test.
    mockedFetchFileContent.mockRejectedValue(new SourceWarmingError(0.001));
    const { result } = renderHook(
      () => useFileSnippet({ sourceFileLink: "/warming.txt", line: 5 }),
      { wrapper: ({ children }) => withQueryClient(children) },
    );

    await waitFor(() => expect(result.current.isWarming).toBe(true), { timeout: 3000 });
    expect(result.current.isSourceUnavailable).toBe(false);
  });

  it("recovers once the tunnel is warm (202 then content)", async () => {
    mockedFetchFileContent
      .mockRejectedValueOnce(new SourceWarmingError(0.001))
      .mockResolvedValueOnce("a\nb\nc\nd\ne");
    const { result } = renderHook(
      () => useFileSnippet({ sourceFileLink: "/recover.txt", line: 3 }),
      { wrapper: ({ children }) => withQueryClient(children) },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true), { timeout: 3000 });
    expect(result.current.snippet).not.toBeNull();
    expect(result.current.isWarming).toBe(false);
  });
});
