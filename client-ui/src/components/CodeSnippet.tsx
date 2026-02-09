import { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";

import { useFileSnippet } from "../lib/snippetCache";

const MonacoEditor = lazy(() => import("@monaco-editor/react"));

type CodeSnippetProps = {
  projectVersionId?: number;
  filePath?: string;
  sourceFileLink?: string;
  line?: number;
  fallback?: string;
};

export default function CodeSnippet({
  projectVersionId,
  filePath,
  sourceFileLink,
  line,
  fallback,
}: CodeSnippetProps) {
  const { snippet, isLoading, isError } = useFileSnippet({
    projectVersionId,
    filePath,
    sourceFileLink,
    line,
  });
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const [editorReady, setEditorReady] = useState(false);
  const editorRef = useRef<any>(null);
  const monacoRef = useRef<any>(null);
  const decorationIds = useRef<string[]>([]);

  const language = useMemo(() => "plaintext", []);

  const lineNumbers = useMemo(() => {
    if (!snippet || expanded) {
      return "on" as const;
    }
    return (lineNumber: number) => String(snippet.start + lineNumber - 1);
  }, [snippet, expanded]);

  const snippetText = useMemo(() => {
    if (!snippet) return "";
    if (expanded) {
      return snippet.fullText;
    }
    return snippet.lines.join("\n");
  }, [snippet, expanded]);

  const highlightLine = useMemo(() => {
    if (!snippet) return null;
    if (expanded) {
      return snippet.highlight;
    }
    return snippet.highlight - snippet.start + 1;
  }, [snippet, expanded]);

  const applyHighlight = () => {
    if (!editorRef.current || !highlightLine || !monacoRef.current) return;
    const editor = editorRef.current;
    const model = editor.getModel();
    if (!model) return;
    const safeLine = Math.min(Math.max(1, highlightLine), model.getLineCount());
    const Range = monacoRef.current.Range;
    decorationIds.current = editor.deltaDecorations(
      decorationIds.current,
      [
        {
          range: new Range(safeLine, 1, safeLine, model.getLineMaxColumn(safeLine)),
          options: {
            isWholeLine: true,
            className: "monaco-highlight-line",
            inlineClassName: "monaco-highlight-line-inline",
            linesDecorationsClassName: "monaco-highlight-line",
            overviewRuler: {
              color: "#4dd4ff",
              position: 2,
            },
          },
        },
      ],
    );
    editor.revealLineInCenter(safeLine);
  };

  useEffect(() => {
    if (!editorReady) return;
    applyHighlight();
  }, [editorReady, highlightLine, snippetText]);

  if ((!filePath && !sourceFileLink) || !line) {
    return (
      <div className="rounded-xl border border-night-500 bg-night-900 px-4 py-3 text-xs text-slate-400">
        Code snippet unavailable.
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="rounded-xl border border-night-500 bg-night-900 p-4">
        <div className="h-4 w-32 animate-pulse rounded bg-night-600"></div>
        <div className="mt-4 space-y-2">
          {Array.from({ length: 6 }).map((_, index) => (
            <div
              key={`skeleton-${index}`}
              className="h-3 w-full animate-pulse rounded bg-night-700"
            ></div>
          ))}
        </div>
      </div>
    );
  }

  if (isError || !snippet) {
    return (
      <div className="rounded-xl border border-night-500 bg-night-900 px-4 py-3 text-xs text-slate-400">
        {fallback ?? "Snippet failed to load."}
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-night-500 bg-night-900 overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-night-500 px-3 py-2 text-xs text-slate-400">
        <span className="min-w-0 flex-1 truncate" title={filePath ?? ""}>
          {filePath ?? "File"}
        </span>
        <div className="flex flex-wrap items-center gap-2">
          <button
            className="rounded-lg border border-night-500 bg-night-700 px-2 py-1 text-xs text-slate-200"
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? "Collapse" : "Expand"}
          </button>
          <button
            className="rounded-lg border border-night-500 bg-night-700 px-2 py-1 text-xs text-slate-200"
            onClick={() => {
              navigator.clipboard.writeText(snippetText);
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            }}
          >
            {copied ? "Copied" : "Copy"}
          </button>
          {highlightLine ? (
            <button
              className="rounded-lg border border-night-500 bg-night-700 px-2 py-1 text-xs text-slate-200"
              onClick={() => {
                if (editorRef.current) {
                  editorRef.current.revealLineInCenter(highlightLine);
                }
              }}
            >
              Jump to line
            </button>
          ) : null}
        </div>
      </div>
      <Suspense
        fallback={
          <div className="px-4 py-3 text-xs text-slate-400">Loading code viewer...</div>
        }
      >
        <MonacoEditor
          height={Math.max(160, (snippetText.split("\n").length + 1) * 18)}
          theme="vs-dark"
          language={language}
          value={snippetText}
          options={{
            readOnly: true,
            minimap: { enabled: false },
            scrollBeyondLastLine: false,
            lineNumbers,
            lineNumbersMinChars: 3,
            glyphMargin: false,
            folding: false,
            renderLineHighlight: "none",
            selectionHighlight: false,
            occurrencesHighlight: "off",
            renderValidationDecorations: "off",
            scrollbar: {
              vertical: "hidden",
              horizontal: "hidden",
            },
            fontSize: 12,
            fontFamily: "IBM Plex Mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
          }}
          onMount={(editor, monaco) => {
            editorRef.current = editor;
            monacoRef.current = monaco;
            setEditorReady(true);
            if (filePath) {
              const lower = filePath.toLowerCase();
              const baseName = lower.split("/").pop() ?? lower;
              const ext = baseName.includes(".") ? `.${baseName.split(".").pop()}` : "";
              const match = monaco.languages
                .getLanguages()
                .find(
                  (lang) =>
                    (ext && lang.extensions?.includes(ext)) ||
                    (lang.filenames && lang.filenames.includes(baseName)),
                );
              if (match?.id && editor.getModel()) {
                monaco.editor.setModelLanguage(editor.getModel()!, match.id);
              }
            }
            applyHighlight();
          }}
        />
      </Suspense>
    </div>
  );
}
