import { useMemo } from "react";
import hljs from "highlight.js";

import { useFileSnippet } from "../lib/snippetCache";
import { useProjectMeta } from "../lib/queries";

type FindingSnippetPreviewProps = {
  projectId?: number;
  projectVersionId?: number;
  filePath?: string;
  sourceFileLink?: string;
  line?: number;
};

export default function FindingSnippetPreview({
  projectId,
  projectVersionId,
  filePath,
  sourceFileLink,
  line,
}: FindingSnippetPreviewProps) {

  const metaQuery = useProjectMeta(projectId);
  const resolvedProjectVersionId =
    projectVersionId ??
    (metaQuery.data?.versions?.length
      ? Number(metaQuery.data.versions[metaQuery.data.versions.length - 1].id)
      : undefined);
  const { snippet, isLoading } = useFileSnippet({
    projectVersionId: resolvedProjectVersionId,
    filePath,
    sourceFileLink,
    line,
  });

  const previewText = useMemo(() => {
    if (!snippet) return null;
    return snippet.lines.slice(0, 3).join("\n");
  }, [snippet]);

  const highlighted = useMemo(() => {
    if (!previewText) return "";
    return hljs.highlightAuto(previewText).value;
  }, [previewText]);

  if ((!filePath && !sourceFileLink) || (!resolvedProjectVersionId && !sourceFileLink)) {
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

  return (
    <div className="rounded-xl border border-night-500 bg-night-900 px-4 py-3 text-xs text-slate-200 snippet-preview-container">
      <pre className="hljs bg-transparent p-0 text-xs font-mono whitespace-pre">
        <code dangerouslySetInnerHTML={{ __html: highlighted }} />
      </pre>
    </div>
  );
}
