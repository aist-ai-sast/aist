import { useId, useMemo, useState, type KeyboardEvent } from "react";
import { Command } from "cmdk";
import { ACCENT_SELECTED_CLASS } from "../lib/uiClasses";
import { isSystemTag } from "../lib/tagRules";
import {
  EMPTY_TAG_FILTER,
  cycleTagState,
  describeTagFilter,
  setTagMatchMode,
  setTagState,
  tagFilterSize,
  tagFilterState,
  toggleTagState,
  type TagFilterValue,
  type TagMatchMode,
} from "../lib/tagFilter";
import FilterClearButton from "./FilterClearButton";
import TagChip from "./TagChip";
import TagComboboxPopover, { Kbd } from "./TagComboboxPopover";

const MATCH_MODES: { value: TagMatchMode; label: string }[] = [
  { value: "any", label: "Any of" },
  { value: "all", label: "All of" },
];

const EXCLUDED_CLASS = "border-danger-500/50 bg-danger-500/10 text-danger-500";
const STATE_ACTION_BUTTON_CLASS =
  "grid h-5 w-5 place-items-center rounded-md border font-mono text-xs leading-none outline-none transition focus-visible:ring-2 focus-visible:ring-brand-600/60";

type TagFilterProps = {
  label?: string;
  options: string[];
  value: TagFilterValue;
  onChange: (value: TagFilterValue) => void;
  emptyLabel?: string;
  /** Findings per tag in the current project scope, shown next to each option. */
  counts?: Record<string, number>;
};

/**
 * Tri-state tag filter: every tag is neutral, included or excluded. The popover
 * shell (Radix Popover + cmdk) is `TagComboboxPopover`, shared with the tag
 * editor; this component only owns the include/exclude model.
 */
export default function TagFilter({
  label = "Tags",
  options,
  value,
  onChange,
  emptyLabel = "No tags available.",
  counts,
}: TagFilterProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [highlighted, setHighlighted] = useState("");
  const labelId = useId();
  const size = tagFilterSize(value);

  // Selected tags first so the current selection is visible without scrolling.
  const orderedOptions = useMemo(() => {
    const rank = (tag: string) => (tagFilterState(value, tag) === "none" ? 1 : 0);
    return [...new Set(options)].sort((left, right) => rank(left) - rank(right) || left.localeCompare(right));
  }, [options, value]);
  const regularOptions = orderedOptions.filter((tag) => !isSystemTag(tag));
  const systemOptions = orderedOptions.filter(isSystemTag);

  const resolveHighlighted = () =>
    options.find((tag) => tag === highlighted)
    ?? options.find((tag) => tag.toLowerCase() === highlighted.toLowerCase());

  // cmdk owns Enter (→ onSelect). Shift+Enter is the direct exclude shortcut and
  // Backspace on an empty search releases the most recent chip.
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Enter" && event.shiftKey) {
      const tag = resolveHighlighted();
      if (!tag) return;
      event.preventDefault();
      onChange(toggleTagState(value, tag, "exclude"));
      return;
    }
    if (event.key === "Backspace" && !query && size > 0) {
      const last = value.exclude[value.exclude.length - 1] ?? value.include[value.include.length - 1];
      event.preventDefault();
      onChange(setTagState(value, last, "none"));
    }
  };

  const renderItem = (tag: string) => {
    const state = tagFilterState(value, tag);
    const actionsVisibility = state === "none"
      ? "opacity-0 group-hover:opacity-100 group-data-[selected=true]:opacity-100 focus-within:opacity-100"
      : "opacity-100";
    return (
      <Command.Item
        key={tag}
        value={tag}
        onSelect={() => onChange(cycleTagState(value, tag))}
        className={[
          "group flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-sm outline-none data-[selected=true]:bg-night-700",
          state === "include" ? "text-brand-100" : state === "exclude" ? "text-danger-500" : "text-slate-200",
        ].join(" ")}
      >
        <span
          aria-hidden="true"
          className={[
            "grid h-4 w-4 place-items-center rounded border text-[11px] leading-none",
            state === "include"
              ? "border-brand-500 bg-brand-500 text-night-900"
              : state === "exclude"
                ? "border-danger-500 bg-danger-500 text-night-900"
                : "border-slate-500",
          ].join(" ")}
        >
          {state === "include" ? "✓" : state === "exclude" ? "−" : ""}
        </span>
        <span className={["flex-1 truncate font-mono text-xs", state === "exclude" ? "line-through" : ""].join(" ")}>
          {tag}
        </span>
        {state !== "none" ? <span className="sr-only">{state === "include" ? "included" : "excluded"}</span> : null}
        {counts?.[tag] !== undefined ? (
          <span className="font-mono text-[11px] tabular-nums text-slate-500">{counts[tag]}</span>
        ) : null}
        <span className={["flex gap-1 transition", actionsVisibility].join(" ")}>
          <button
            type="button"
            aria-label={`Include ${tag}`}
            aria-pressed={state === "include"}
            className={[
              STATE_ACTION_BUTTON_CLASS,
              state === "include" ? ACCENT_SELECTED_CLASS : "border-night-500 bg-night-800 text-slate-300 hover:text-white",
            ].join(" ")}
            onClick={(event) => {
              event.stopPropagation();
              onChange(toggleTagState(value, tag, "include"));
            }}
          >
            +
          </button>
          <button
            type="button"
            aria-label={`Exclude ${tag}`}
            aria-pressed={state === "exclude"}
            className={[
              STATE_ACTION_BUTTON_CLASS,
              state === "exclude" ? EXCLUDED_CLASS : "border-night-500 bg-night-800 text-slate-300 hover:text-white",
            ].join(" ")}
            onClick={(event) => {
              event.stopPropagation();
              onChange(toggleTagState(value, tag, "exclude"));
            }}
          >
            −
          </button>
        </span>
      </Command.Item>
    );
  };

  const trigger = (
    <button
      type="button"
      aria-labelledby={labelId}
      className="mt-2 flex h-10 w-full items-center gap-2 rounded-xl border border-night-500 bg-night-600 px-3 text-left text-sm text-white outline-none transition focus-visible:border-brand-600 focus-visible:ring-2 focus-visible:ring-brand-600/60 data-[state=open]:border-brand-600 data-[state=open]:ring-2 data-[state=open]:ring-brand-600/60"
    >
      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 shrink-0 text-slate-400" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z" />
        <circle cx="7" cy="7" r="1.5" />
      </svg>
      <span className={["flex-1 truncate", size > 0 ? "text-slate-100" : "text-slate-400"].join(" ")}>
        {size > 0 ? describeTagFilter(value) : "Any tag"}
      </span>
      <svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor" className="shrink-0 text-slate-400" aria-hidden="true">
        <path d="M5.25 7.5 10 12.25 14.75 7.5H5.25Z" />
      </svg>
    </button>
  );

  return (
    <div>
      <div className="flex items-center justify-between gap-2">
        <span id={labelId} className="text-xs text-slate-400">
          {label}
          {size > 0 ? (
            <span className="ml-1.5 rounded-full border border-brand-500/60 bg-brand-500/20 px-1.5 text-[10px] tabular-nums text-brand-100">
              {size}
            </span>
          ) : null}
        </span>
        {size > 0 ? (
          <FilterClearButton onClick={() => onChange({ ...EMPTY_TAG_FILTER, matchMode: value.matchMode })} />
        ) : null}
      </div>

      <TagComboboxPopover
        label={label}
        open={open}
        onOpenChange={setOpen}
        trigger={trigger}
        query={query}
        onQueryChange={setQuery}
        highlighted={highlighted}
        onHighlightedChange={setHighlighted}
        onKeyDown={handleKeyDown}
        header={
          <div className="flex items-center justify-between gap-2 border-b border-night-500 px-3 py-2 text-xs text-slate-400">
            <span>Match included tags</span>
            <div role="group" aria-label="Match mode" className="inline-flex rounded-full border border-night-500 bg-night-800 p-0.5">
              {MATCH_MODES.map((mode) => (
                <button
                  key={mode.value}
                  type="button"
                  aria-pressed={value.matchMode === mode.value}
                  className={[
                    "rounded-full px-2.5 py-0.5 text-[11px] transition",
                    value.matchMode === mode.value ? "bg-brand-500/20 text-brand-100" : "text-slate-300 hover:text-white",
                  ].join(" ")}
                  onClick={() => onChange(setTagMatchMode(value, mode.value))}
                >
                  {mode.label}
                </button>
              ))}
            </div>
          </div>
        }
        footer={
          <div className="flex items-center justify-between gap-2 border-t border-night-500 px-3 py-2 text-[11px] text-slate-500">
            <span className="flex items-center gap-1">
              <Kbd>↵</Kbd> cycle <Kbd>⇧↵</Kbd> exclude
            </span>
            <button
              type="button"
              className="text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-300 transition hover:text-brand-300"
              onClick={() => setOpen(false)}
            >
              Done
            </button>
          </div>
        }
      >
        {options.length === 0 ? (
          <div className="px-3 py-4 text-center text-xs text-slate-500">{emptyLabel}</div>
        ) : (
          <Command.Empty className="px-3 py-4 text-center text-xs text-slate-500">
            No tags match “{query}”
          </Command.Empty>
        )}
        {regularOptions.map(renderItem)}
        {systemOptions.length > 0 ? (
          <Command.Group
            heading="System"
            className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:text-[10px] [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-[0.12em] [&_[cmdk-group-heading]]:text-slate-500"
          >
            {systemOptions.map(renderItem)}
          </Command.Group>
        ) : null}
      </TagComboboxPopover>

      {size > 0 ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {value.include.map((tag) => (
            <TagChip
              key={`include:${tag}`}
              tag={tag}
              variant="include"
              onRemove={() => onChange(setTagState(value, tag, "none"))}
            />
          ))}
          {value.exclude.map((tag) => (
            <TagChip
              key={`exclude:${tag}`}
              tag={tag}
              variant="exclude"
              onRemove={() => onChange(setTagState(value, tag, "none"))}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

