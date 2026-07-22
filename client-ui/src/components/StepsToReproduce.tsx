import { useState } from "react";

import { parseStepsToReproduce, type StepPart } from "../lib/dastNarrative";
import { renderMarkdownInline } from "../lib/markdown";
import DescriptionBlock from "./DescriptionBlock";

type StepsToReproduceProps = {
  raw?: string | null;
};

function stripWrappingBackticks(text: string): string {
  const trimmed = text.trim();
  return trimmed.startsWith("`") && trimmed.endsWith("`") && trimmed.length > 1
    ? trimmed.slice(1, -1)
    : trimmed;
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="absolute right-2 top-2 rounded-md border border-white/10 bg-white/5 px-2 py-1 text-[11px] font-medium text-slate-300 transition hover:bg-white/10 hover:text-white"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        });
      }}
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

function CalloutIcon({ label }: { label: StepPart["label"] }) {
  if (label === "Request") {
    return (
      <svg viewBox="0 0 24 24" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <polyline points="4 17 10 11 4 5" />
        <line x1="12" y1="19" x2="20" y2="19" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <line x1="12" y1="8" x2="12" y2="13" />
      <line x1="12" y1="16" x2="12.01" y2="16" />
    </svg>
  );
}

function StepPartBlock({ part }: { part: StepPart }) {
  if (part.label === "action") {
    return (
      <div
        className="text-sm text-slate-100"
        dangerouslySetInnerHTML={{ __html: renderMarkdownInline(part.text) }}
      />
    );
  }
  if (part.label === "Request") {
    const command = stripWrappingBackticks(part.text);
    return (
      <div>
        <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.1em] text-slate-400">
          <CalloutIcon label={part.label} />
          Request
        </div>
        <div className="relative rounded-lg border border-night-500 bg-black/30 px-3 py-2 pr-16">
          <code className="block whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-brand-100">
            {command}
          </code>
          <CopyButton text={command} />
        </div>
      </div>
    );
  }
  return (
    <div>
      <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.1em] text-slate-400">
        <CalloutIcon label={part.label} />
        {part.label}
      </div>
      <div
        className="rounded-lg border border-brand-600/25 bg-brand-500/[0.06] px-3 py-2 text-sm text-slate-200"
        dangerouslySetInnerHTML={{ __html: renderMarkdownInline(part.text) }}
      />
    </div>
  );
}

export default function StepsToReproduce({ raw }: StepsToReproduceProps) {
  if (!raw || !raw.trim()) {
    return (
      <div className="rounded-xl border border-night-500 bg-night-900 px-4 py-3 text-sm text-slate-400">
        No steps to reproduce were reported.
      </div>
    );
  }

  const steps = parseStepsToReproduce(raw);
  if (!steps) {
    // Not the numbered-step convention this parser recognizes — never hide the
    // data, just fall back to the same safe markdown rendering used for descriptions.
    return (
      <div className="rounded-xl border border-night-500 bg-night-900 px-4 py-3">
        <DescriptionBlock value={raw} />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      {steps.map((step, index) => (
        <div key={index} className="flex gap-3 rounded-xl border border-night-500 bg-night-900 p-3">
          <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-brand-600 font-mono text-xs font-bold text-night-900">
            {index + 1}
          </div>
          <div className="flex min-w-0 flex-1 flex-col gap-2">
            {step.parts.map((part, partIndex) => (
              <StepPartBlock key={partIndex} part={part} />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
