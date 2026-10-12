import { ACCENT_SELECTED_CLASS } from "../lib/uiClasses";
import type { ActiveFilterTone } from "../lib/findingsActiveFilters";
import FilterClearButton from "./FilterClearButton";

/**
 * What the bar needs to render a chip. Each page keeps its own chip type — findings carry
 * a findings-shaped removal patch, pipelines a pipelines-shaped one — and the bar stays
 * generic over it, handing the whole chip back to `onRemove` untouched.
 */
export type RenderableFilterChip = {
  id: string;
  label: string;
  value: string;
  tone: ActiveFilterTone;
};

type ActiveFiltersBarProps<TChip extends RenderableFilterChip> = {
  chips: TChip[];
  onRemove: (chip: TChip) => void;
  onClearAll: () => void;
};

const TONE_CLASS: Record<ActiveFilterTone, string> = {
  neutral: "border-night-500 bg-night-600 text-slate-200",
  include: ACCENT_SELECTED_CLASS,
  exclude: "border-danger-500/50 bg-danger-500/10 text-danger-500",
};

/** Mirrors the filter panel above the results so exclusions stay visible when the panel is scrolled or collapsed. */
export default function ActiveFiltersBar<TChip extends RenderableFilterChip>({
  chips,
  onRemove,
  onClearAll,
}: ActiveFiltersBarProps<TChip>) {
  if (chips.length === 0) return null;
  return (
    <div
      aria-label="Active filters"
      className="flex flex-wrap items-center gap-2 rounded-2xl border border-night-500 bg-night-800 px-3 py-2"
    >
      <span className="text-[11px] uppercase tracking-[0.12em] text-slate-500">Active filters</span>
      {chips.map((chip) => (
        <span
          key={chip.id}
          className={["inline-flex items-center gap-1.5 rounded-full border py-0.5 pl-2.5 pr-1 text-xs", TONE_CLASS[chip.tone]].join(" ")}
        >
          <span className="text-slate-400">{chip.label}</span>
          <span className={chip.tone === "exclude" ? "line-through" : ""}>{chip.value}</span>
          <button
            type="button"
            aria-label={`Remove filter ${chip.label} ${chip.value}`}
            className="grid h-4 w-4 place-items-center rounded-full text-[11px] leading-none hover:bg-white/10"
            onClick={() => onRemove(chip)}
          >
            ×
          </button>
        </span>
      ))}
      <FilterClearButton className="ml-auto" label="Clear all" onClick={onClearAll} />
    </div>
  );
}
