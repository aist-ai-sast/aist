import { useCallback, useId, useMemo, useState } from "react";
import * as Popover from "@radix-ui/react-popover";
import * as Select from "@radix-ui/react-select";
import { Command } from "cmdk";

type Option = {
  value: string;
  label: string;
  disabled?: boolean;
};

const NONE_SENTINEL = "__none__";

/**
 * Lists this long stop being readable by scrolling. The longest enumerations in the app
 * (pipeline statuses, work-item statuses) stay below it on purpose: a fixed set of states
 * is read, not searched, and a search field would only raise the keyboard on phones.
 * Server-driven lists — projects, branches, CWEs, DAST targets — pass `searchable`
 * explicitly rather than relying on how many rows happen to exist today.
 */
export const SEARCHABLE_OPTION_THRESHOLD = 12;

const TRIGGER_CLASS =
  "flex h-10 w-full items-center justify-between gap-2 rounded-xl border border-night-500 bg-night-600 px-3 text-left text-sm text-white outline-none transition focus-visible:border-brand-600 focus-visible:ring-2 focus-visible:ring-brand-600/60 data-[state=open]:border-brand-600 data-[state=open]:ring-2 data-[state=open]:ring-brand-600/60";

type SelectFieldProps = {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: Option[];
  disabled?: boolean;
  placeholder?: string;
  hideLabel?: boolean;
  showIndicator?: boolean;
  side?: "top" | "bottom" | "left" | "right";
  clearable?: boolean;
  clearLabel?: string;
  /** Defaults to on once the list passes `SEARCHABLE_OPTION_THRESHOLD`. */
  searchable?: boolean;
  searchPlaceholder?: string;
};

function ChevronIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor" className="shrink-0 text-slate-400" aria-hidden="true">
      <path d="M5.25 7.5 10 12.25 14.75 7.5H5.25Z" />
    </svg>
  );
}

/**
 * Searchable variant: Radix Popover owns positioning, dismissal and focus, cmdk owns
 * filtering, keyboard navigation and the empty state — the same pair, and the same visual
 * grammar, as `TagFilter`. This component only maps between option values and the trigger.
 */
function SearchableSelect({
  label,
  value,
  onChange,
  options,
  disabled,
  placeholder,
  labelId,
  hideLabel,
  side,
  clearable,
  clearLabel,
  searchPlaceholder,
}: Required<Pick<SelectFieldProps, "label" | "value" | "onChange" | "options" | "placeholder" | "clearable" | "clearLabel">> &
  Pick<SelectFieldProps, "disabled" | "hideLabel" | "side" | "searchPlaceholder"> & { labelId: string }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  const selected = useMemo(() => options.find((option) => option.value === value), [options, value]);

  // cmdk normalises item values (it lower-cases them), so the search runs against labels
  // looked up here rather than against the value cmdk hands back, and every `onSelect`
  // reports the option captured in its own closure instead of that normalised value.
  const labelByValue = useMemo(() => {
    const map = new Map(options.map((option) => [option.value.toLowerCase(), option.label]));
    map.set(NONE_SENTINEL, clearLabel);
    return map;
  }, [options, clearLabel]);

  const filterOption = useCallback(
    (itemValue: string, search: string) => {
      const needle = search.trim().toLowerCase();
      if (!needle) return 1;
      const label = labelByValue.get(itemValue.toLowerCase()) ?? itemValue;
      return label.toLowerCase().includes(needle) ? 1 : 0;
    },
    [labelByValue],
  );

  function close() {
    setOpen(false);
    setQuery("");
  }

  function pick(next: string) {
    onChange(next === NONE_SENTINEL ? "" : next);
    close();
  }

  return (
    <Popover.Root
      open={open}
      onOpenChange={(next) => (next ? setOpen(true) : close())}
    >
      <Popover.Trigger asChild disabled={disabled}>
        <button
          type="button"
          aria-labelledby={hideLabel ? undefined : labelId}
          aria-label={hideLabel ? label : undefined}
          disabled={disabled}
          className={[TRIGGER_CLASS, hideLabel ? "mt-0" : "mt-2", disabled ? "opacity-60" : ""].join(" ")}
        >
          <span className={["min-w-0 flex-1 truncate", selected ? "text-white" : "text-slate-400"].join(" ")}>
            {selected?.label ?? placeholder}
          </span>
          <ChevronIcon />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="start"
          side={side}
          sideOffset={8}
          // Matches TagFilter: above every overlay in the app, so the list never renders
          // behind a drawer or another popover that hosts this field.
          className="z-[1700] w-[var(--radix-popover-trigger-width)] min-w-[220px] max-w-[calc(100vw-2rem)] overflow-hidden rounded-xl border border-night-500 bg-night-900 shadow-panel"
        >
          <Command label={label} filter={filterOption} loop>
            <div className="flex items-center gap-2 border-b border-night-500 px-3 py-2">
              <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 shrink-0 text-slate-400" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <circle cx="11" cy="11" r="7" />
                <path d="M21 21l-4.3-4.3" />
              </svg>
              <Command.Input
                value={query}
                onValueChange={setQuery}
                placeholder={searchPlaceholder ?? `Search ${label.toLowerCase()}…`}
                className="h-8 flex-1 bg-transparent text-sm text-white outline-none placeholder:text-slate-500"
              />
            </div>
            <Command.List className="aist-scrollbar max-h-64 overflow-y-auto p-1">
              <Command.Empty className="px-3 py-4 text-center text-xs text-slate-500">
                No match for “{query}”
              </Command.Empty>
              {clearable && (
                <Command.Item
                  value={NONE_SENTINEL}
                  onSelect={() => pick(NONE_SENTINEL)}
                  className="cursor-pointer select-none rounded-lg px-3 py-2 text-sm italic text-slate-400 outline-none data-[selected=true]:bg-night-700"
                >
                  {clearLabel}
                </Command.Item>
              )}
              {options.map((option) => (
                <Command.Item
                  key={option.value}
                  value={option.value}
                  disabled={option.disabled}
                  // The guard is ours rather than cmdk's: a disabled option must not be
                  // selectable regardless of how the list library treats `disabled`.
                  onSelect={() => {
                    if (option.disabled) return;
                    pick(option.value);
                  }}
                  className="flex cursor-pointer select-none items-center gap-2 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none data-[selected=true]:bg-night-700 data-[disabled=true]:text-slate-500"
                >
                  <span className="w-3.5 shrink-0 text-brand-500" aria-hidden="true">
                    {option.value === value ? "✓" : ""}
                  </span>
                  <span className="min-w-0 flex-1 truncate">{option.label}</span>
                </Command.Item>
              ))}
            </Command.List>
          </Command>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

export default function SelectField({
  label,
  value,
  onChange,
  options,
  disabled,
  placeholder = "Select",
  hideLabel = false,
  showIndicator = true,
  side,
  clearable = false,
  clearLabel = "None",
  searchable,
  searchPlaceholder,
}: SelectFieldProps) {
  const labelId = useId();
  const withSearch = searchable ?? options.length >= SEARCHABLE_OPTION_THRESHOLD;

  function handleChange(v: string) {
    onChange(v === NONE_SENTINEL ? "" : v);
  }

  return (
    <div>
      {hideLabel ? null : <span id={labelId} className="text-xs text-slate-400">{label}</span>}
      {withSearch ? (
        <SearchableSelect
          label={label}
          labelId={labelId}
          value={value}
          onChange={onChange}
          options={options}
          disabled={disabled}
          placeholder={placeholder}
          hideLabel={hideLabel}
          side={side}
          clearable={clearable}
          clearLabel={clearLabel}
          searchPlaceholder={searchPlaceholder}
        />
      ) : (
        <Select.Root value={value || undefined} onValueChange={handleChange} disabled={disabled}>
          <Select.Trigger
            aria-labelledby={hideLabel ? undefined : labelId}
            className={[TRIGGER_CLASS, hideLabel ? "mt-0" : "mt-2"].join(" ")}
          >
            <Select.Value className="min-w-0 truncate" placeholder={placeholder} />
            {showIndicator ? (
              <Select.Icon className="text-slate-400">
                <ChevronIcon />
              </Select.Icon>
            ) : null}
          </Select.Trigger>
          <Select.Portal>
            <Select.Content
              position="popper"
              side={side}
              // Above the highest overlay z-index in the app (the Manage-access
              // drawer at z-[80]) so the dropdown never renders behind it.
              className="z-[100] max-w-[var(--radix-select-content-available-width)] overflow-hidden rounded-xl border border-night-500 bg-night-900 shadow-panel"
              style={{ minWidth: "var(--radix-select-trigger-width)" }}
            >
              <Select.Viewport className="p-1">
                {clearable && (
                  <Select.Item
                    value={NONE_SENTINEL}
                    className="cursor-pointer select-none rounded-lg px-3 py-2 text-sm text-slate-400 italic outline-none data-[highlighted]:bg-night-700 data-[state=checked]:bg-night-600"
                  >
                    <Select.ItemText>{clearLabel}</Select.ItemText>
                  </Select.Item>
                )}
                {options.map((option) => (
                  <Select.Item
                    key={option.value}
                    value={option.value}
                    disabled={option.disabled}
                    className="cursor-pointer select-none rounded-lg px-3 py-2 text-sm text-slate-200 outline-none data-[highlighted]:bg-night-700 data-[state=checked]:bg-night-600 data-[disabled]:text-slate-500"
                  >
                    <Select.ItemText>{option.label}</Select.ItemText>
                  </Select.Item>
                ))}
              </Select.Viewport>
            </Select.Content>
          </Select.Portal>
        </Select.Root>
      )}
    </div>
  );
}
