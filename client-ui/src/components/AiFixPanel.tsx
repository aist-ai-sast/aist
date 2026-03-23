import { Suspense, lazy, useState } from "react";

import type { AiFix } from "../types";
import { ACCENT_SELECTED_CLASS } from "../lib/uiClasses";

const MonacoEditor = lazy(() => import("@monaco-editor/react"));

function filenameFromDiff(diff?: string | null): string | undefined {
  if (!diff) return undefined;
  const match = diff.match(/^(?:---|\+\+\+)\s+[ab]\/(.+?)(?:\s|$)/m);
  return match ? match[1].split("/").pop() : undefined;
}

type InnerTab = "diff" | "code" | "steps";
type SecondarySection = "testing" | "suppress" | null;

const FIX_TYPE_BADGE: Record<string, string> = {
  code_change: "border-brand-500/40 bg-brand-500/10 text-brand-200",
  config_change: "border-amber-400/40 bg-amber-400/10 text-amber-200",
  architectural: "border-purple-400/40 bg-purple-400/10 text-purple-200",
};

const FIX_TYPE_LABEL: Record<string, string> = {
  code_change: "Code Change",
  config_change: "Config Change",
  architectural: "Architectural",
};

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="rounded-lg border border-night-500 bg-night-700 px-2 py-1 text-xs text-slate-200 shrink-0"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch {
          /* clipboard unavailable */
        }
      }}
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

function DiffView({ diff }: { diff: string }) {
  const lines = diff.split("\n");
  return (
    <div className="space-y-2">
      <pre className="overflow-x-auto rounded-xl border border-night-500 bg-night-900 p-3 text-xs font-mono leading-5">
        {lines.map((line, i) => {
          let cls = "text-slate-400";
          if (line.startsWith("+") && !line.startsWith("+++")) cls = "text-emerald-300";
          else if (line.startsWith("-") && !line.startsWith("---")) cls = "text-danger-200";
          else if (line.startsWith("@@")) cls = "text-brand-300";
          else if (line.startsWith("---") || line.startsWith("+++")) cls = "text-slate-300";
          return (
            <div key={i} className={cls}>
              {line || "\u00a0"}
            </div>
          );
        })}
      </pre>
      <div className="flex justify-end">
        <CopyButton text={diff} />
      </div>
    </div>
  );
}

function CodeAfterView({ code, path }: { code: string; path?: string }) {
  const lineCount = code.split("\n").length;
  const height = Math.max(120, Math.min(lineCount * 18 + 16, 400));
  return (
    <div className="space-y-2">
      <div className="overflow-hidden rounded-xl border border-night-500">
        <Suspense fallback={
          <div className="bg-night-900 px-4 py-3 text-xs text-slate-400">Loading...</div>
        }>
          <MonacoEditor
            height={height}
            theme="vs-dark"
            path={path}
            value={code}
            options={{
              readOnly: true,
              automaticLayout: true,
              minimap: { enabled: false },
              scrollBeyondLastLine: false,
              lineNumbers: "on",
              lineNumbersMinChars: 3,
              glyphMargin: false,
              folding: false,
              renderLineHighlight: "none",
              selectionHighlight: false,
              occurrencesHighlight: "off",
              renderValidationDecorations: "off",
              scrollbar: { vertical: "auto", horizontal: "auto" },
              fontSize: 12,
              fontFamily: "IBM Plex Mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
            }}
          />
        </Suspense>
      </div>
      <div className="flex justify-end">
        <CopyButton text={code} />
      </div>
    </div>
  );
}

function StepsView({ steps }: { steps: string[] }) {
  return (
    <ol className="space-y-2">
      {steps.map((step, i) => (
        <li key={i} className="flex gap-3 text-xs text-slate-200">
          <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-night-500 bg-night-800 text-[10px] text-slate-400">
            {i + 1}
          </span>
          <span className="leading-relaxed">{step.replace(/^Step\s+\d+:\s*/i, "")}</span>
        </li>
      ))}
    </ol>
  );
}

export default function AiFixPanel({ fix }: { fix: AiFix }) {
  const [open, setOpen] = useState(false);
  const [innerTab, setInnerTab] = useState<InnerTab>("diff");
  const [section, setSection] = useState<SecondarySection>(null);
  const codePath = filenameFromDiff(fix.diff);

  const availableTabs: Array<{ id: InnerTab; label: string }> = [
    ...(fix.diff ? [{ id: "diff" as const, label: "Diff" }] : []),
    ...(fix.codeAfter ? [{ id: "code" as const, label: "Code After" }] : []),
    { id: "steps" as const, label: "Steps" },
  ];

  const activeTab = availableTabs.find((t) => t.id === innerTab)
    ? innerTab
    : (availableTabs[0]?.id ?? "steps");

  const badgeClass = FIX_TYPE_BADGE[fix.fixType] ?? "border-night-500 bg-night-800 text-slate-300";
  const badgeLabel = FIX_TYPE_LABEL[fix.fixType] ?? fix.fixType;

  return (
    <div className="rounded-xl border border-night-500 bg-night-800/60">
      {/* Header — always visible */}
      <div className="flex items-start justify-between gap-3 px-4 py-3">
        <div className="flex min-w-0 flex-col gap-1.5">
          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase tracking-[0.15em] text-slate-400">
              Fix available
            </span>
            <span className={`rounded-full border px-2 py-0.5 text-[10px] font-medium ${badgeClass}`}>
              {badgeLabel}
            </span>
          </div>
          <p className="text-xs leading-relaxed text-slate-200">{fix.fixSummary}</p>
        </div>
        <button
          type="button"
          aria-expanded={open}
          className="flex shrink-0 items-center gap-1 rounded-lg border border-night-500 bg-night-700 px-2.5 py-1.5 text-xs text-slate-200 transition hover:border-brand-600/40"
          onClick={() => setOpen((v) => !v)}
        >
          {open ? "Hide" : "Show fix"}
          <svg
            viewBox="0 0 24 24"
            className={`h-3 w-3 transition-transform ${open ? "rotate-180" : ""}`}
            fill="none"
            stroke="currentColor"
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>
      </div>

      {/* Expanded content */}
      {open ? (
        <div className="space-y-3 border-t border-night-500 px-4 pb-4 pt-3">
          {/* Inner tab pills */}
          <div className="flex flex-wrap gap-2">
            {availableTabs.map((t) => (
              <button
                key={t.id}
                type="button"
                className={[
                  "rounded-full border px-3 py-1 text-xs",
                  activeTab === t.id
                    ? ACCENT_SELECTED_CLASS
                    : "border-night-500 bg-night-900 text-slate-300",
                ].join(" ")}
                onClick={() => setInnerTab(t.id)}
              >
                {t.label}
              </button>
            ))}
          </div>

          {/* Inner tab content */}
          {activeTab === "diff" && fix.diff ? (
            <DiffView diff={fix.diff} />
          ) : activeTab === "code" && fix.codeAfter ? (
            <CodeAfterView code={fix.codeAfter} path={codePath} />
          ) : (
            <StepsView steps={fix.stepByStep} />
          )}

          {/* Secondary pills */}
          {(fix.testingHint || fix.secretsManagement || fix.suppressionAnnotation) ? (
            <div className="flex flex-wrap gap-2 border-t border-night-500/50 pt-3">
              {fix.testingHint ? (
                <button
                  type="button"
                  className={[
                    "rounded-full border px-3 py-1 text-xs",
                    section === "testing"
                      ? ACCENT_SELECTED_CLASS
                      : "border-night-500 bg-night-900 text-slate-300",
                  ].join(" ")}
                  onClick={() => setSection(section === "testing" ? null : "testing")}
                >
                  How to test
                </button>
              ) : null}
              {fix.suppressionAnnotation ? (
                <button
                  type="button"
                  className={[
                    "rounded-full border px-3 py-1 text-xs",
                    section === "suppress"
                      ? ACCENT_SELECTED_CLASS
                      : "border-night-500 bg-night-900 text-slate-300",
                  ].join(" ")}
                  onClick={() => setSection(section === "suppress" ? null : "suppress")}
                >
                  Suppress annotation
                </button>
              ) : null}
            </div>
          ) : null}

          {section === "testing" && fix.testingHint ? (
            <div className="rounded-xl border border-night-500 bg-night-900 px-3 py-3 text-xs leading-relaxed text-slate-200">
              {fix.testingHint}
            </div>
          ) : null}

          {section === "suppress" && fix.suppressionAnnotation ? (
            <div className="rounded-xl border border-night-500 bg-night-900 px-3 py-3 space-y-2">
              <p className="text-xs text-slate-400">
                Paste this comment above the flagged line in your source code:
              </p>
              <div className="flex items-center gap-2">
                <code className="min-w-0 flex-1 overflow-x-auto rounded-lg border border-night-500 bg-night-800 px-2 py-1 font-mono text-xs text-slate-200">
                  {fix.suppressionAnnotation}
                </code>
                <CopyButton text={fix.suppressionAnnotation} />
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
