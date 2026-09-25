import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { ApiError, isSourceWarmingError } from "./api";
import { fetchFileContent } from "./api";

// While a VPN egress tunnel warms up the backend answers 202; the first view of
// an idle tunnel can take up to ~30s, so poll a bounded number of times.
const WARMING_MAX_RETRIES = 15;
const DEFAULT_RETRY_DELAY_MS = 500;
const DEFAULT_MAX_RETRIES = 2;

// The blob endpoint answers 502 with one of these codes when the SCM itself refused
// or failed the fetch (as opposed to the file being absent, which is a 404).
type ScmErrorCode = "scm_auth_failed" | "scm_unavailable";

const SCM_ERROR_MESSAGES: Record<ScmErrorCode, string> = {
  scm_auth_failed:
    "The repository rejected the organization's SCM integration token. Ask an organization admin to update it in the integration settings.",
  scm_unavailable: "The source repository is temporarily unavailable. Try again later.",
};

function scmErrorCode(error: unknown): ScmErrorCode | null {
  if (!(error instanceof ApiError) || error.status !== 502) return null;
  let payload = error.payload;
  // File content is requested as text, so an error body arrives as an unparsed string.
  if (typeof payload === "string") {
    try {
      payload = JSON.parse(payload);
    } catch {
      return null;
    }
  }
  const code = (payload as { code?: unknown } | null)?.code;
  return code === "scm_auth_failed" || code === "scm_unavailable" ? code : null;
}

type SnippetParams = {
  sourceFileLink?: string;
  line?: number;
  context?: number;
  // When false the snippet is not fetched yet (e.g. row not scrolled into view).
  enabled?: boolean;
};

const PREVIEW_LINE_COUNT = 12;

export function useFileSnippet({ sourceFileLink, line, context = 3, enabled: enabledProp = true }: SnippetParams) {
  const enabled = enabledProp && Boolean(sourceFileLink);

  const query = useQuery({
    queryKey: ["file", sourceFileLink],
    queryFn: () => fetchFileContent(sourceFileLink!),
    enabled,
    retry: (failureCount, error) => {
      if (isSourceWarmingError(error)) return failureCount < WARMING_MAX_RETRIES;
      // A rejected credential stays rejected until an admin rotates it; retrying only
      // repeats the failed authentication against the SCM.
      if (scmErrorCode(error) === "scm_auth_failed") return false;
      return failureCount < DEFAULT_MAX_RETRIES;
    },
    retryDelay: (_failureCount, error) =>
      isSourceWarmingError(error) ? error.retryAfter * 1000 : DEFAULT_RETRY_DELAY_MS,
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
  });

  // `failureReason` carries the in-flight error while retries are still pending;
  // fall back to the final `error` once retries are exhausted.
  const isWarming = isSourceWarmingError(query.failureReason) || isSourceWarmingError(query.error);

  const snippet = useMemo(() => {
    if (!query.data) return null;
    const lines = query.data.split(/\r?\n/);
    const hasHighlight = typeof line === "number" && line > 0;
    const start = hasHighlight ? Math.max(1, line - context) : 1;
    const end = hasHighlight ? Math.min(lines.length, line + context) : Math.min(lines.length, PREVIEW_LINE_COUNT);
    const slice = lines.slice(start - 1, end);
    return {
      start,
      end,
      lines: slice,
      highlight: hasHighlight ? line : null,
      fullText: query.data,
      hasHighlight,
    };
  }, [query.data, line, context]);

  const isSourceUnavailable = query.error instanceof ApiError && query.error.status === 404;
  const scmError = scmErrorCode(query.error);
  const scmErrorMessage = scmError ? SCM_ERROR_MESSAGES[scmError] : null;

  return {
    ...query,
    snippet,
    isSourceUnavailable,
    isWarming,
    scmErrorMessage,
  };
}
