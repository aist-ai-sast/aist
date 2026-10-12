import { ACCENT_SELECTED_CLASS } from "../lib/uiClasses";

type FilterToggleButtonProps = {
  open: boolean;
  onToggle: () => void;
  /** Number of filters currently applied; shown as a badge when non-zero. */
  activeCount: number;
};

/**
 * The one control that opens and closes a page's filter panel. Shared by Dashboard,
 * Findings and Pipelines so the three pages cannot drift apart again; the open/closed
 * state itself lives in `useFilterPanelOpen`.
 */
export default function FilterToggleButton({ open, onToggle, activeCount }: FilterToggleButtonProps) {
  return (
    <button
      type="button"
      className="aist-icon-button h-9 px-3 text-xs font-semibold uppercase tracking-[0.14em]"
      aria-pressed={open}
      onClick={onToggle}
    >
      <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
        <path fill="currentColor" d="M10 18h4v-2h-4v2Zm-7-10v2h18V8H3Zm3 7h12v-2H6v2Z" />
      </svg>
      Filters
      {activeCount > 0 ? (
        <span
          className={`grid h-[18px] min-w-[18px] place-items-center rounded-full border px-1 text-[10px] tracking-normal ${ACCENT_SELECTED_CLASS}`}
          aria-label={`${activeCount} active`}
        >
          {activeCount}
        </span>
      ) : null}
    </button>
  );
}
