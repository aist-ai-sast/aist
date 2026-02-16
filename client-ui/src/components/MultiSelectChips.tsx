import { useState } from "react";
import { ACCENT_SELECTED_CLASS } from "../lib/uiClasses";

type MultiSelectChipsProps = {
  label: string;
  options: string[];
  selected: string[];
  onChange: (value: string[]) => void;
  emptyLabel?: string;
  visibleCount?: number;
};

export default function MultiSelectChips({
  label,
  options,
  selected,
  onChange,
  emptyLabel = "No options available.",
  visibleCount = 12,
}: MultiSelectChipsProps) {
  const [expanded, setExpanded] = useState(false);
  const visibleOptions = expanded ? options : options.slice(0, visibleCount);
  return (
    <div>
      <label className="text-xs text-slate-400">{label}</label>
      <div className="mt-2 flex flex-wrap gap-2">
        {options.length ? (
          visibleOptions.map((option) => (
            <button
              key={option}
              type="button"
              className={[
                "rounded-full border px-3 py-1 text-xs",
                selected.includes(option)
                  ? ACCENT_SELECTED_CLASS
                  : "border-night-500 bg-night-600 text-slate-200 hover:border-night-400",
              ].join(" ")}
              onClick={() => {
                if (selected.includes(option)) {
                  onChange(selected.filter((item) => item !== option));
                } else {
                  onChange([...selected, option]);
                }
              }}
            >
              {option}
            </button>
          ))
        ) : (
          <div className="text-xs text-slate-500">{emptyLabel}</div>
        )}
        {options.length > visibleCount ? (
          <button
            type="button"
            className="rounded-full border border-night-500 bg-night-600 px-3 py-1 text-xs text-slate-300"
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? "Show fewer" : `Show all (${options.length})`}
          </button>
        ) : null}
      </div>
    </div>
  );
}
