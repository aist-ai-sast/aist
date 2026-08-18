import { useMemo, useState } from "react";

import { usePipelineDastRun } from "../lib/queries";
import {
  bucketLabel,
  bucketShare,
  bucketsBySpend,
  formatCompactTokens,
  formatCount,
  formatDuration,
  formatShare,
} from "../lib/dastRun";
import type { DastRunSummary, DastTokenBucket } from "../types";

const CHART_ACCENTS = [
  "var(--aist-chart-accent-1)",
  "var(--aist-chart-accent-2)",
  "var(--aist-chart-accent-4)",
  "var(--aist-chart-accent-3)",
  "var(--aist-chart-accent-6)",
  "var(--aist-chart-accent-5)",
  "rgba(148, 163, 184, 0.75)",
  "rgba(100, 116, 139, 0.7)",
];

const TOKEN_COUNTERS: Array<{ key: keyof DastRunDetailTokens; label: string }> = [
  { key: "input_tokens", label: "Input" },
  { key: "output_tokens", label: "Output" },
  { key: "thinking_tokens", label: "Thinking" },
  { key: "cache_creation_tokens", label: "Cache write" },
  { key: "cache_read_tokens", label: "Cache read" },
];

type DastRunDetailTokens = {
  input_tokens: number | null;
  output_tokens: number | null;
  thinking_tokens: number | null;
  cache_creation_tokens: number | null;
  cache_read_tokens: number | null;
};

/** A field is rendered only when the report carried it — never as a zero or a dash. */
function Field({ label, value }: { label: string; value: string | null }) {
  if (value === null) return null;
  return (
    <div className="min-w-0">
      <span className="block text-[10px] uppercase tracking-[0.18em] text-slate-500">{label}</span>
      <span className="block break-words text-xs leading-snug text-slate-100">{value}</span>
    </div>
  );
}

function Panel({ title, aside, children }: { title: string; aside?: string | null; children: React.ReactNode }) {
  return (
    <section className="rounded-xl border border-night-500 bg-night-900 px-4 py-3">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
        <span className="text-xs font-semibold text-slate-100">{title}</span>
        {aside ? <span className="text-[11px] text-slate-400">{aside}</span> : null}
      </div>
      {children}
    </section>
  );
}

/**
 * Compact counters for the collapsed pipeline row. Each chip appears only when its own
 * numbers are present, so a report that skipped a block simply has one fewer chip.
 */
export function DastRunChips({ run }: { run: DastRunSummary }) {
  const kind = run.runType ? `DAST · ${run.runType}` : "DAST";
  const unit = run.coverageUnit ? `${run.coverageUnit}s` : "units";
  const coverage =
    run.analysed !== null && run.reachable !== null
      ? `${formatCount(run.analysed)} / ${formatCount(run.reachable)} ${unit} analysed`
      : run.analysed !== null
        ? `${formatCount(run.analysed)} ${unit} analysed`
        : null;
  const tokens = formatCompactTokens(run.totalTokens);

  return (
    <div className="mt-3 flex flex-wrap items-center gap-2">
      <span className="inline-flex items-center gap-2 rounded-full border border-brand-600/35 bg-brand-600/10 px-3 py-1 text-xs text-brand-500">
        <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />
        {kind}
      </span>
      {coverage ? (
        <span className="rounded-full border border-night-500 bg-night-900 px-3 py-1 text-xs text-slate-200">
          {coverage}
        </span>
      ) : null}
      {tokens ? (
        <span className="rounded-full border border-night-500 bg-night-900 px-3 py-1 text-xs text-slate-200">
          {tokens} tokens
        </span>
      ) : null}
      {run.beyondPlan !== null && run.beyondPlan > 0 ? (
        <span className="rounded-full border border-amber-400/35 bg-amber-400/10 px-3 py-1 text-xs text-amber-400">
          {formatCount(run.beyondPlan)} beyond plan
        </span>
      ) : null}
    </div>
  );
}

function CoverageBar({ label, value, of, note }: { label: string; value: number; of: number; note: string | null }) {
  const width = of > 0 ? Math.min((value / of) * 100, 100) : 0;
  const isPlan = label === "Planned";
  return (
    <div className="grid grid-cols-[84px_1fr_auto] items-center gap-3">
      <span className="text-[11px] text-slate-300">{label}</span>
      <div className="h-[22px] overflow-hidden rounded-[5px] bg-night-500/45">
        <div
          className={[
            "h-full rounded-[5px]",
            isPlan ? "border-r-2 border-amber-400 bg-amber-400/25" : "bg-brand-500/70",
          ].join(" ")}
          style={{ width: `${width}%` }}
        />
      </div>
      <span className="min-w-[112px] text-right text-xs text-slate-100">
        {formatCount(value)}
        {note ? <span className="ml-1 text-[11px] text-slate-400">{note}</span> : null}
      </span>
    </div>
  );
}

function SegmentedBar({ buckets, total, kind }: { buckets: DastTokenBucket[]; total: number | null; kind: "phase" | "agent" }) {
  return (
    <div className="mt-4 flex h-2.5 overflow-hidden rounded-full bg-night-500/50">
      {buckets.map((bucket, index) => {
        const share = bucketShare(bucket, total);
        if (share === null) return null;
        return (
          <span
            key={bucket.key}
            style={{ width: `${share}%`, background: CHART_ACCENTS[index % CHART_ACCENTS.length] }}
            title={`${bucketLabel(bucket, kind)} — ${formatCount(bucket.total_tokens)} tokens (${formatShare(share)})`}
          />
        );
      })}
    </div>
  );
}

function BucketTable({ buckets, total, kind }: { buckets: DastTokenBucket[]; total: number | null; kind: "phase" | "agent" }) {
  const showAgents = buckets.some((bucket) => bucket.agents !== null && bucket.agents !== undefined);
  return (
    <div className="mt-3 overflow-x-auto">
      <table className="w-full min-w-[420px] border-collapse text-[11px]">
        <thead>
          <tr className="border-b border-night-500">
            {[kind === "phase" ? "Phase" : "Agent type", ...(showAgents ? ["Agents"] : []), "Calls", "Tokens", "Share"].map(
              (heading, index) => (
                <th
                  key={heading}
                  className={[
                    "py-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500",
                    index === 0 ? "text-left" : "text-right pl-3",
                  ].join(" ")}
                >
                  {heading}
                </th>
              ),
            )}
          </tr>
        </thead>
        <tbody>
          {buckets.map((bucket, index) => (
            <tr key={bucket.key} className={index > 0 ? "border-t border-night-500/45" : ""}>
              <td className="flex items-center gap-2 py-2 pr-3 text-slate-100">
                <span
                  className="h-2.5 w-2.5 shrink-0 rounded-[3px]"
                  style={{ background: CHART_ACCENTS[index % CHART_ACCENTS.length] }}
                  aria-hidden="true"
                />
                <span className="break-words">{bucketLabel(bucket, kind)}</span>
              </td>
              {showAgents ? <td className="py-2 pl-3 text-right text-slate-200">{formatCount(bucket.agents) ?? "—"}</td> : null}
              <td className="py-2 pl-3 text-right text-slate-200">{formatCount(bucket.calls) ?? "—"}</td>
              <td className="py-2 pl-3 text-right text-slate-200">{formatCompactTokens(bucket.total_tokens) ?? "—"}</td>
              <td className="py-2 pl-3 text-right text-slate-400">{formatShare(bucketShare(bucket, total)) ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * A report may legitimately carry thousands of names, and a scroll container caps the height
 * without capping the node count. Only this many are put in the DOM at once; the count that
 * matched is always stated, so a trimmed list reads as trimmed rather than as the whole set.
 */
const INVENTORY_RENDER_LIMIT = 250;

function EndpointInventory({ names, beyondPlan, unit }: { names: string[]; beyondPlan: Set<string>; unit: string }) {
  const [query, setQuery] = useState("");
  const [beyondOnly, setBeyondOnly] = useState(false);

  const matching = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return names.filter(
      (name) => (!needle || name.toLowerCase().includes(needle)) && (!beyondOnly || beyondPlan.has(name)),
    );
  }, [names, query, beyondOnly, beyondPlan]);

  const visible = matching.slice(0, INVENTORY_RENDER_LIMIT);
  const hidden = matching.length - visible.length;

  return (
    <Panel
      title={`${unit} analysed (${formatCount(names.length)})`}
      aside={hidden > 0 ? `showing ${visible.length} of ${formatCount(matching.length)}` : `showing ${visible.length}`}
    >
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={`Filter ${unit.toLowerCase()}…`}
          aria-label={`Filter analysed ${unit.toLowerCase()}`}
          className="min-w-[160px] flex-1 rounded-lg border border-night-500 bg-night-800 px-3 py-1.5 text-xs text-slate-100 placeholder:text-slate-500"
        />
        {beyondPlan.size > 0 ? (
          <button
            type="button"
            aria-pressed={beyondOnly}
            onClick={() => setBeyondOnly((current) => !current)}
            className={[
              "rounded-lg border px-3 py-1.5 text-[11px] transition",
              beyondOnly
                ? "border-amber-400/50 bg-amber-400/10 text-amber-400"
                : "border-night-500 bg-night-800 text-slate-300 hover:border-brand-500/45 hover:text-slate-100",
            ].join(" ")}
          >
            Beyond plan only ({beyondPlan.size})
          </button>
        ) : null}
      </div>
      {hidden > 0 ? (
        <p className="mb-2 text-[11px] text-slate-400">
          Showing the first {visible.length} of {formatCount(matching.length)} matches — narrow the filter to
          reach the rest.
        </p>
      ) : null}
      <div className="aist-scrollbar flex max-h-44 flex-wrap content-start gap-1.5 overflow-y-auto pr-1">
        {visible.length === 0 ? (
          <span className="py-1 text-[11px] text-slate-500">No {unit.toLowerCase()} matches that filter.</span>
        ) : (
          visible.map((name) => {
            const beyond = beyondPlan.has(name);
            return (
              <span
                key={name}
                title={beyond ? "Analysed beyond the run plan" : undefined}
                className={[
                  "inline-flex max-w-full items-center gap-1.5 break-all rounded-full border px-2.5 py-0.5 font-mono text-[11px]",
                  beyond ? "border-amber-400/40 text-amber-200/90" : "border-night-500 bg-night-800/75 text-slate-300",
                ].join(" ")}
              >
                {beyond ? <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400" aria-hidden="true" /> : null}
                {name}
              </span>
            );
          })
        )}
      </div>
    </Panel>
  );
}

/**
 * Coverage and token usage of the report accepted onto this pipeline. Gated on the presence of
 * the metadata rather than on the execution type, so an operator upload — which is a
 * MANUAL_IMPORT pipeline and carries no DAST outcome — still shows its run.
 */
export default function DastRunPanels({ pipelineId }: { pipelineId: string }) {
  const [breakdown, setBreakdown] = useState<"phase" | "agent">("phase");
  const dastRunQuery = usePipelineDastRun(pipelineId);
  const run = dastRunQuery.data;

  if (dastRunQuery.isLoading) {
    return <div className="rounded-xl border border-night-500 bg-night-900 px-4 py-3 text-xs text-slate-400">Loading run metadata…</div>;
  }
  if (!run) return null;

  const unitLabel = run.coverageUnit ? `${run.coverageUnit.charAt(0).toUpperCase()}${run.coverageUnit.slice(1)}s` : "Targets";
  const coverageStages: Array<{ label: string; value: number | null }> = [
    { label: "Discovered", value: run.discovered },
    { label: "Reachable", value: run.reachable },
    { label: "Analysed", value: run.analysed },
    { label: "Planned", value: run.planned },
  ];
  const presentStages = coverageStages.filter((stage) => stage.value !== null);
  const scale = Math.max(...presentStages.map((stage) => stage.value ?? 0), 1);

  const buckets = bucketsBySpend(breakdown === "phase" ? run.tokenByPhase : run.tokenByAgentType);
  const hasTokens = run.totalTokens !== null || run.modelCalls !== null || buckets.length > 0;
  const tokenCounters = TOKEN_COUNTERS.filter(({ key }) => run.tokens[key] !== null);

  return (
    <div className="flex flex-col gap-3.5">
      <Panel title="Run">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <Field label="Run ID" value={run.runId} />
          <Field label="Stand" value={run.standId} />
          <Field label="Target" value={run.targetId} />
          <Field label="Entry host" value={run.targetHost} />
          <Field label="Family" value={run.productFamily} />
          <Field label="Tier" value={run.tier} />
          <Field label="Run type" value={run.runType} />
          <Field label="Duration" value={formatDuration(run.durationSeconds)} />
        </div>
      </Panel>

      {presentStages.length > 0 ? (
        <Panel title="Scan coverage" aside={run.coverageUnit ? `unit: ${run.coverageUnit}` : null}>
          <div className="flex flex-col gap-2.5">
            {presentStages.map((stage) => (
              <CoverageBar
                key={stage.label}
                label={stage.label}
                value={stage.value as number}
                of={scale}
                note={
                  stage.label === "Reachable" && run.discovered
                    ? `${Math.round(((stage.value as number) / run.discovered) * 100)}% of discovered`
                    : stage.label === "Analysed" && run.reachable
                      ? `${Math.round(((stage.value as number) / run.reachable) * 100)}% of reachable`
                      : stage.label === "Planned"
                        ? "plan for this run"
                        : null
                }
              />
            ))}
          </div>
          {run.beyondPlan !== null && run.beyondPlan > 0 ? (
            <div className="mt-3.5 flex flex-wrap items-center gap-2 border-t border-night-500/70 pt-3 text-[11px] text-slate-300">
              <span className="inline-flex items-center gap-2 rounded-full border border-amber-400/35 bg-amber-400/10 px-3 py-1 text-amber-400">
                <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />
                {formatCount(run.beyondPlan)} analysed beyond plan
              </span>
              <span>The run went past its own plan.</span>
            </div>
          ) : null}
        </Panel>
      ) : null}

      {run.analysedNames ? (
        <EndpointInventory
          names={run.analysedNames}
          beyondPlan={new Set(run.beyondPlanNames ?? [])}
          unit={unitLabel}
        />
      ) : null}

      {hasTokens ? (
        <Panel title="Agent token usage">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <span className="block text-[10px] uppercase tracking-[0.18em] text-slate-500">Total tokens</span>
              <span className="text-2xl font-semibold text-white">
                {formatCompactTokens(run.totalTokens) ?? "—"}
                {run.modelCalls !== null ? (
                  <small className="ml-2 text-xs font-normal text-slate-400">
                    across {formatCount(run.modelCalls)} model calls
                  </small>
                ) : null}
              </span>
            </div>
            {run.tokenByPhase && run.tokenByAgentType ? (
              <div className="flex gap-1" role="tablist" aria-label="Token breakdown">
                {(["phase", "agent"] as const).map((mode) => (
                  <button
                    key={mode}
                    type="button"
                    role="tab"
                    aria-selected={breakdown === mode}
                    onClick={() => setBreakdown(mode)}
                    className={[
                      "rounded-lg border px-3 py-1 text-[11px] transition",
                      breakdown === mode
                        ? "border-brand-500/35 bg-brand-500/10 text-brand-500"
                        : "border-transparent text-slate-400 hover:text-slate-100",
                    ].join(" ")}
                  >
                    By {mode}
                  </button>
                ))}
              </div>
            ) : null}
          </div>

          {tokenCounters.length > 0 ? (
            <div className="mt-3.5 flex flex-wrap gap-x-6 gap-y-2">
              {tokenCounters.map(({ key, label }) => (
                <Field key={key} label={label} value={formatCount(run.tokens[key])} />
              ))}
              <Field label="Agents" value={formatCount(run.agents)} />
            </div>
          ) : null}

          {buckets.length > 0 ? (
            <>
              <SegmentedBar buckets={buckets} total={run.totalTokens} kind={breakdown} />
              <BucketTable buckets={buckets} total={run.totalTokens} kind={breakdown} />
            </>
          ) : null}

          {run.tokenAccountingConsistent === false ? (
            <p className="mt-3 rounded-lg border border-amber-400/30 bg-amber-400/10 px-3 py-2 text-[11px] text-amber-400">
              Accounting mismatch: the reported breakdown does not add up to the reported total. The findings
              are unaffected — only these numbers should be read with care.
            </p>
          ) : null}
        </Panel>
      ) : null}
    </div>
  );
}
