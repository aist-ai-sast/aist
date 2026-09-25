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
  // Production scenario: the organization's GitLab PAT expired and the blob endpoint
  // answers 502 {"code":"scm_auth_failed"}. The body arrives as text (blobs are fetched
  // with responseType "text"), so the hook must parse it.
  function scmError(code: string) {
    return new ApiError({
      status: 502,
      code: "http_error",
      payload: JSON.stringify({ detail: "The source code repository rejected ...", code }),
      url: "/api/v2/aist/projects_version/60/files/blob/cloud/cloud/settings.py",
    });
  }

  it("explains an expired SCM integration token and does not retry it", async () => {
    mockedFetchFileContent.mockReset();
    mockedFetchFileContent.mockRejectedValue(scmError("scm_auth_failed"));
    const { result } = renderHook(
      () => useFileSnippet({ sourceFileLink: "/expired-token.txt", line: 5 }),
      { wrapper: ({ children }) => withQueryClient(children) },
    );

    await waitFor(() => expect(result.current.isError).toBe(true), { timeout: 3000 });
    expect(result.current.scmErrorMessage).toMatch(/SCM integration token/);
    expect(result.current.isSourceUnavailable).toBe(false);
    expect(result.current.isWarming).toBe(false);
    // Retrying a rejected credential only repeats the failed login against the SCM.
    expect(mockedFetchFileContent).toHaveBeenCalledTimes(1);
  });

  it("explains a temporarily unavailable SCM", async () => {
    mockedFetchFileContent.mockReset();
    mockedFetchFileContent.mockRejectedValue(scmError("scm_unavailable"));
    const { result } = renderHook(
      () => useFileSnippet({ sourceFileLink: "/scm-down.txt", line: 5 }),
      { wrapper: ({ children }) => withQueryClient(children) },
    );

    await waitFor(() => expect(result.current.isError).toBe(true), { timeout: 5000 });
    expect(result.current.scmErrorMessage).toMatch(/temporarily unavailable/);
  });

  it("does not claim an SCM problem for an unrelated 502 (e.g. proxy HTML page)", async () => {
    mockedFetchFileContent.mockReset();
    mockedFetchFileContent.mockRejectedValue(
      new ApiError({ status: 502, code: "http_error", payload: "<html>Bad Gateway</html>", url: "/proxy.txt" }),
    );
    const { result } = renderHook(
      () => useFileSnippet({ sourceFileLink: "/proxy.txt", line: 5 }),
      { wrapper: ({ children }) => withQueryClient(children) },
    );

    await waitFor(() => expect(result.current.isError).toBe(true), { timeout: 5000 });
    expect(result.current.scmErrorMessage).toBeNull();
  });
});
