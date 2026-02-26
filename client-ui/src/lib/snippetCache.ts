import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { ApiError } from "./api";
import { fetchFileContent } from "./api";

type SnippetParams = {
  sourceFileLink?: string;
  line?: number;
  context?: number;
};

const PREVIEW_LINE_COUNT = 12;

export function useFileSnippet({ sourceFileLink, line, context = 3 }: SnippetParams) {
  const enabled = Boolean(sourceFileLink);

  const query = useQuery({
    queryKey: ["file", sourceFileLink],
    queryFn: () => fetchFileContent(sourceFileLink!),
    enabled,
    retry: 2,
    retryDelay: 500,
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
  });

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

  return {
    ...query,
    snippet,
    isSourceUnavailable,
  };
}
