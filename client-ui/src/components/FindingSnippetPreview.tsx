import { useMemo } from "react";
import hljs from "highlight.js";
import DOMPurify from "dompurify";

import { useFileSnippet } from "../lib/snippetCache";

type FindingSnippetPreviewProps = {
  filePath?: string;
  sourceFileLink?: string;
  line?: number | null;
};

export default function FindingSnippetPreview({
  filePath,
  sourceFileLink,
  line,
}: FindingSnippetPreviewProps) {
  void filePath;
  const { snippet, isLoading, isError, isSourceUnavailable } = useFileSnippet({
    sourceFileLink,
    line: line ?? undefined,
  });

  const previewText = useMemo(() => {
    if (!snippet) return null;
    return snippet.lines.slice(0, 3).join("\n");
  }, [snippet]);

  const highlighted = useMemo(() => {
    if (!previewText) return "";
    return DOMPurify.sanitize(hljs.highlightAuto(previewText).value);
  }, [previewText]);

  if (!sourceFileLink) {
    return (
      <div className="rounded-xl border border-night-500 bg-night-900 px-4 py-3 font-mono text-xs text-slate-400">
        Snippet preview unavailable
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="rounded-xl border border-night-500 bg-night-900 px-4 py-3 text-xs text-slate-400">
        Loading snippet...
      </div>
    );
  }

  if (isError || !snippet) {
    return (
      <div className="rounded-xl border border-night-500 bg-night-900 px-4 py-3 font-mono text-xs text-slate-400">
        {isSourceUnavailable
          ? "Source file is unavailable for this project version."
          : "Snippet preview unavailable"}
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-night-500 bg-night-900 px-4 py-3 text-xs text-slate-200 snippet-preview-container">
      <pre className="hljs bg-transparent p-0 text-xs font-mono whitespace-pre">
        <code dangerouslySetInnerHTML={{ __html: highlighted }} />
      </pre>
    </div>
  );
}
