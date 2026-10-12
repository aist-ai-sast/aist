import { useState } from "react";

import SelectField from "./SelectField";
import TextInput from "./TextInput";
import type { FindingCloseReason } from "../lib/mutations";

export type BulkAction = "close" | "reopen" | "risk_accept";

export const BULK_ACTION_OPTIONS: { value: string; label: string }[] = [
  { value: "close", label: "Close Findings" },
  { value: "reopen", label: "Reopen Findings" },
  { value: "risk_accept", label: "Risk Accept Findings" },
];

export const BULK_CLOSE_REASON_OPTIONS: { value: string; label: string }[] = [
  { value: "mitigated", label: "Mitigated" },
  { value: "false_positive", label: "False Positive" },
  { value: "out_of_scope", label: "Out of Scope" },
  { value: "duplicate", label: "Duplicate" },
];

export type BulkEditBarProps = {
  action: BulkAction;
  onActionChange: (action: BulkAction) => void;
  closeReason: FindingCloseReason;
  onCloseReasonChange: (reason: FindingCloseReason) => void;
  reasonNote: string;
  onReasonNoteChange: (note: string) => void;
  /** False when the product forbids full risk acceptance; the banner explains it. */
  riskAcceptEnabled: boolean;
  /** Everything selected, including findings on pages that are no longer rendered. */
  selectedCount: number;
  /** Selectable findings on the current page only. */
  visibleSelectableCount: number;
  allVisibleSelected: boolean;
  onToggleSelectVisible: () => void;
  onClearSelection: () => void;
  onExit: () => void;
  onApply: () => void;
  canApply: boolean;
  pending: boolean;
};

const LABEL_CLASS = "mb-1 block text-xs uppercase tracking-[0.18em] text-slate-400";
const BUTTON_CLASS = "aist-icon-button h-10 text-xs font-semibold uppercase tracking-[0.14em] disabled:opacity-50";
const APPLY_CLASS = "h-10 rounded-xl bg-brand-500 px-4 text-xs font-semibold uppercase tracking-[0.14em] text-night-900 disabled:opacity-50";

function RiskAcceptBanner({ enabled }: { enabled: boolean }) {
  return enabled ? (
    <div className="flex items-center gap-2 rounded-xl border border-amber-400/30 bg-amber-400/8 px-3 py-2 text-xs text-amber-300">
      <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 3 5 6v6c0 4.2 2.8 8.1 7 9 4.2-.9 7-4.8 7-9V6l-7-3Z" />
        <path d="m9.5 12 1.7 1.7 3.3-3.4" />
      </svg>
      Risk Accept is a compliance decision recorded under your name. Ensure the justification meets your policy requirements before applying.
    </div>
  ) : (
    <div className="flex items-center gap-2 rounded-xl border border-danger-500/30 bg-danger-500/8 px-3 py-2 text-xs text-danger-300">
      <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 3 5 6v6c0 4.2 2.8 8.1 7 9 4.2-.9 7-4.8 7-9V6l-7-3Z" />
        <path d="M12 8v4M12 16h.01" />
      </svg>
      Full risk acceptance is not enabled for this product. Contact your product owner to enable it.
    </div>
  );
}

/**
 * Bulk-edit controls, in the two shapes the screen allows.
 *
 * From `sm` up this is the panel it has always been, sitting in the flow above the results.
 * Below `sm` that panel is about 400 px tall and covers every finding it is meant to act
 * on, so there the controls become a bar pinned above the phone navigation: collapsed it
 * states the selection and applies, expanded it holds the same fields and the same
 * selection actions. Nothing is dropped between the two — in particular "Clear" empties the
 * whole selection, including findings on pages that have since been paged away, which
 * "Deselect visible" deliberately does not.
 */
export default function BulkEditBar(props: BulkEditBarProps) {
  const {
    action,
    onActionChange,
    closeReason,
    onCloseReasonChange,
    reasonNote,
    onReasonNoteChange,
    riskAcceptEnabled,
    selectedCount,
    visibleSelectableCount,
    allVisibleSelected,
    onToggleSelectVisible,
    onClearSelection,
    onExit,
    onApply,
    canApply,
    pending,
  } = props;

  const [phoneExpanded, setPhoneExpanded] = useState(false);

  const actionLabel = BULK_ACTION_OPTIONS.find((option) => option.value === action)?.label ?? action;
  const closeReasonLabel = BULK_CLOSE_REASON_OPTIONS.find((option) => option.value === closeReason)?.label;
  const summary = action === "close" && closeReasonLabel ? `${actionLabel} · ${closeReasonLabel}` : actionLabel;
  const selectVisibleLabel = allVisibleSelected
    ? `Deselect Visible (${visibleSelectableCount})`
    : `Select Visible (${visibleSelectableCount})`;

  const fields = (
    <>
      <div className="min-w-0 flex-1 sm:min-w-[180px]">
        <label className={LABEL_CLASS}>BULK ACTION</label>
        <SelectField
          label="BULK ACTION"
          value={action}
          onChange={(value) => onActionChange(value as BulkAction)}
          options={BULK_ACTION_OPTIONS}
          hideLabel
        />
      </div>
      {action === "close" ? (
        <div className="min-w-0 flex-1 sm:min-w-[220px]">
          <label className={LABEL_CLASS}>CLOSE AS</label>
          <SelectField
            label="CLOSE AS"
            value={closeReason}
            onChange={(value) => onCloseReasonChange(value as FindingCloseReason)}
            options={BULK_CLOSE_REASON_OPTIONS}
            hideLabel
          />
        </div>
      ) : null}
      <div className="min-w-0 flex-[2] sm:min-w-[260px]">
        <label className={LABEL_CLASS}>{action === "risk_accept" ? "JUSTIFICATION" : "REASON"}</label>
        <TextInput
          value={reasonNote}
          onChange={(event) => onReasonNoteChange(event.target.value)}
          placeholder={action === "risk_accept" ? "Enter risk justification" : "Enter reason for audit log"}
        />
      </div>
      {action === "risk_accept" ? (
        <div className="w-full">
          <RiskAcceptBanner enabled={riskAcceptEnabled} />
        </div>
      ) : null}
    </>
  );

  return (
    <>
      {/* ── sm and up: the panel in the flow ───────────────────────────────── */}
      <div className="hidden rounded-2xl border border-night-500 bg-night-700/95 p-4 shadow-panel sm:block">
        <div className="mb-3 flex items-center justify-between gap-2 text-xs uppercase tracking-[0.18em] text-slate-400">
          <span>Bulk Edit</span>
          <span>Selected: {selectedCount}</span>
        </div>
        <div className="flex flex-wrap items-end gap-3">
          {fields}
          <button
            type="button"
            className={BUTTON_CLASS}
            disabled={visibleSelectableCount === 0 || pending}
            onClick={onToggleSelectVisible}
          >
            {selectVisibleLabel}
          </button>
          <button
            type="button"
            className={BUTTON_CLASS}
            disabled={selectedCount === 0 || pending}
            onClick={onClearSelection}
          >
            Clear
          </button>
          <button type="button" className={BUTTON_CLASS} disabled={pending} onClick={onExit}>
            Hide Bulk Edit
          </button>
          <button type="button" className={APPLY_CLASS} disabled={!canApply} onClick={onApply}>
            Apply to {selectedCount}
          </button>
        </div>
      </div>

      {/* ── below sm: a bar above the phone navigation (which is h-16) ─────── */}
      <div
        className="fixed inset-x-0 bottom-16 z-[59] px-4 sm:hidden"
        role="region"
        aria-label="Bulk edit"
      >
        <div className="aist-scrollbar max-h-[70vh] overflow-y-auto rounded-2xl border border-brand-500/45 bg-night-700 p-3 shadow-panel">
          <div className="flex items-center gap-3">
            <button
              type="button"
              className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-slate-300 transition hover:bg-night-600 hover:text-white disabled:opacity-50"
              aria-label="Exit bulk edit"
              disabled={pending}
              onClick={onExit}
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
                <path fill="currentColor" d="m7.41 6 4.59 4.59L16.59 6 18 7.41 13.41 12 18 16.59 16.59 18 12 13.41 7.41 18 6 16.59 10.59 12 6 7.41 7.41 6Z" />
              </svg>
            </button>
            <button
              type="button"
              className="min-w-0 flex-1 text-left"
              aria-expanded={phoneExpanded}
              onClick={() => setPhoneExpanded((current) => !current)}
            >
              <span className="flex items-center gap-1.5 text-sm font-semibold text-white">
                {selectedCount} selected
                <svg
                  viewBox="0 0 24 24"
                  className={["h-4 w-4 text-slate-400 transition-transform", phoneExpanded ? "" : "rotate-180"].join(" ")}
                  aria-hidden="true"
                >
                  <path fill="currentColor" d="M7 14l5-5 5 5H7Z" />
                </svg>
              </span>
              <span className="block truncate text-xs text-slate-400">{summary}</span>
            </button>
            <button
              type="button"
              className="h-9 shrink-0 rounded-xl bg-brand-500 px-3 text-xs font-semibold uppercase tracking-[0.14em] text-night-900 disabled:opacity-50"
              disabled={!canApply}
              onClick={onApply}
            >
              Apply
            </button>
          </div>

          {phoneExpanded ? (
            <div className="mt-3 flex flex-col gap-3 border-t border-night-500 pt-3">
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  className="rounded-lg border border-night-500 bg-night-800 px-3 py-1.5 text-[11px] text-slate-300 transition hover:border-brand-500/45 hover:text-slate-100 disabled:opacity-50"
                  disabled={visibleSelectableCount === 0 || pending}
                  onClick={onToggleSelectVisible}
                >
                  {selectVisibleLabel}
                </button>
                <button
                  type="button"
                  className="rounded-lg border border-night-500 bg-night-800 px-3 py-1.5 text-[11px] text-slate-300 transition hover:border-brand-500/45 hover:text-slate-100 disabled:opacity-50"
                  disabled={selectedCount === 0 || pending}
                  onClick={onClearSelection}
                >
                  Clear all ({selectedCount})
                </button>
              </div>
              <div className="flex flex-col gap-3">{fields}</div>
            </div>
          ) : null}
        </div>
      </div>
    </>
  );
}
