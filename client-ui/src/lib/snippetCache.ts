import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { fetchFileContent } from "./api";

type SnippetParams = {
  projectVersionId?: number;
  filePath?: string;
  sourceFileLink?: string;
  line?: number;
  context?: number;
};

export function useFileSnippet({
  projectVersionId,
  filePath,
  sourceFileLink,
  line,
  context = 3,
}: SnippetParams) {
  const enabled = Boolean(sourceFileLink || (projectVersionId && filePath));

  const query = useQuery({
    queryKey: ["file", sourceFileLink ?? projectVersionId, filePath],
    queryFn: () =>
      sourceFileLink ? fetchFileContent(sourceFileLink) : fetchFileContent(projectVersionId!, filePath!),
    enabled,
    retry: 2,
    retryDelay: 500,
    refetchOnWindowFocus: false,
  });

  const snippet = useMemo(() => {
    if (!query.data || !line) return null;
    const lines = query.data.split(/\r?\n/);
    const start = Math.max(1, line - context);
    const end = Math.min(lines.length, line + context);
    const slice = lines.slice(start - 1, end);
    return {
      start,
      end,
      lines: slice,
      highlight: line,
      fullText: query.data,
    };
  }, [query.data, line, context]);

  return {
    ...query,
    snippet,
  };
}
