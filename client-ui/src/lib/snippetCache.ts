import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { fetchFileContent } from "./api";

type SnippetParams = {
  sourceFileLink?: string;
  line?: number;
  context?: number;
};

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
