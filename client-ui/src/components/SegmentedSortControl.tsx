import { ACCENT_SELECTED_CLASS } from "../lib/uiClasses";

type SortDirection = "asc" | "desc";

type SortOption<T extends string> = {
  value: T;
  label: string;
};

type SegmentedSortControlProps<T extends string> = {
  options: Array<SortOption<T>>;
  value: T;
  direction: SortDirection;
  onValueChange: (value: T) => void;
  onDirectionToggle: () => void;
};

export default function SegmentedSortControl<T extends string>({
  options,
  value,
  direction,
  onValueChange,
  onDirectionToggle,
}: SegmentedSortControlProps<T>) {
  return (
    <div className="flex w-full flex-col items-stretch gap-2 sm:w-auto sm:flex-row sm:items-end">
      <div className="inline-flex h-10 items-center rounded-xl border border-night-500 bg-night-800 p-1">
        {options.map((option) => (
          <button
            key={option.value}
            type="button"
            className={[
              "h-8 rounded-lg px-3 text-xs font-medium tracking-wide transition",
              value === option.value
                ? `border ${ACCENT_SELECTED_CLASS} text-white`
                : "border border-night-500 bg-night-800 text-slate-200 hover:border-brand-500/40",
            ].join(" ")}
            onClick={() => onValueChange(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>
      <button
        type="button"
        className="aist-icon-button h-10 min-w-[76px] justify-center"
        title={`Sort direction: ${direction.toUpperCase()}`}
        onClick={onDirectionToggle}
      >
        <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
          <path
            fill="currentColor"
            d={direction === "desc"
              ? "M7 6h10v2H7V6Zm0 5h7v2H7v-2Zm0 5h4v2H7v-2Zm9 4-3.5-4h2.5v-9h2v9H19.5L16 20Z"
              : "M7 6h4v2H7V6Zm0 5h7v2H7v-2Zm0 5h10v2H7v-2Zm9-12 3.5 4H17v9h-2V8h-2.5L16 4Z"}
          />
        </svg>
        {direction.toUpperCase()}
      </button>
    </div>
  );
}
