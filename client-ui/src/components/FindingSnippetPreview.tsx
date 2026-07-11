import { useEffect, useMemo, useRef, useState } from "react";
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

  // Viewport-lazy: only fetch once the row scrolls into view.  A findings page
  // renders up to 50 rows; without this every row would fetch its file at once
  // (and warm/hit the VPN egress) even for rows the user never looks at.
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [isVisible, setIsVisible] = useState(false);
  useEffect(() => {
    if (isVisible) return; // already revealed — fetch stays enabled
    const node = containerRef.current;
    if (!node || typeof IntersectionObserver === "undefined") {
      setIsVisible(true); // no observer (e.g. tests) → fetch eagerly
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) setIsVisible(true);
    }, { rootMargin: "200px" });
    observer.observe(node);
    return () => observer.disconnect();
  }, [isVisible]);

  const { snippet, isLoading, isError, isSourceUnavailable, isWarming } = useFileSnippet({
    sourceFileLink,
    line: line ?? undefined,
    enabled: isVisible,
  });

  const previewText = useMemo(() => {
    if (!snippet) return null;
    return snippet.lines.slice(0, 3).join("\n");
  }, [snippet]);

  const highlighted = useMemo(() => {
    if (!previewText) return "";
    return DOMPurify.sanitize(hljs.highlightAuto(previewText).value);
  }, [previewText]);

  const baseClass = "rounded-xl border border-night-500 bg-night-900 px-4 py-3 text-xs";

  const renderInner = () => {
    if (!sourceFileLink) {
      return <span className="font-mono text-slate-400">Snippet preview unavailable</span>;
    }
    // `isWarming` is checked before the generic error branch: while the VPN
    // egress warms up the backend answers 202 and the hook keeps retrying.
    if (isWarming) {
      return <span className="text-slate-400">Loading source over VPN…</span>;
    }
    if (!isVisible || isLoading) {
      return <span className="text-slate-400">Loading snippet...</span>;
    }
    if (isError || !snippet) {
      return (
        <span className="font-mono text-slate-400">
          {isSourceUnavailable
            ? "Source file is unavailable for this project version."
            : "Snippet preview unavailable"}
        </span>
      );
    }
    return (
      <pre className="hljs bg-transparent p-0 text-xs font-mono whitespace-pre text-slate-200">
        <code dangerouslySetInnerHTML={{ __html: highlighted }} />
      </pre>
    );
  };

  return (
    <div ref={containerRef} className={`${baseClass} snippet-preview-container`}>
      {renderInner()}
    </div>
  );
}
