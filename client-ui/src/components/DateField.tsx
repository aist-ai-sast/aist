import { useMemo, useState } from "react";
import * as Popover from "@radix-ui/react-popover";
import { DayPicker } from "react-day-picker";
import { format, isValid, parseISO } from "date-fns";

type DateFieldProps = {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
};

export default function DateField({
  label,
  value,
  onChange,
  placeholder = "Select date",
}: DateFieldProps) {
  const [open, setOpen] = useState(false);
  const selectedDate = useMemo(() => {
    if (!value) return undefined;
    const parsed = parseISO(value);
    return isValid(parsed) ? parsed : undefined;
  }, [value]);

  const display = selectedDate ? format(selectedDate, "dd.MM.yy") : placeholder;

  return (
    <div>
      <label className="text-xs text-slate-400">{label}</label>
      <Popover.Root open={open} onOpenChange={setOpen}>
        <Popover.Trigger asChild>
          <button
            type="button"
            className="mt-2 flex h-10 w-full items-center justify-between rounded-xl border border-night-500 bg-night-600 px-3 text-sm text-white outline-none transition focus-visible:border-brand-600 focus-visible:ring-2 focus-visible:ring-brand-600/60"
          >
            <span className={value ? "text-white" : "text-slate-400"}>{display}</span>
            <svg viewBox="0 0 24 24" className="h-4 w-4 text-slate-400" aria-hidden="true">
              <path
                fill="currentColor"
                d="M7 2h2v2h6V2h2v2h2a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2V2Zm12 8H5v8h14v-8ZM5 6v2h14V6H5Z"
              />
            </svg>
          </button>
        </Popover.Trigger>
        <Popover.Content
          align="start"
          sideOffset={8}
          className="rounded-2xl border border-night-500 bg-night-900 p-3 shadow-panel"
        >
          <DayPicker
            mode="single"
            selected={selectedDate}
            onSelect={(date) => {
              if (!date) {
                onChange("");
              } else {
                onChange(format(date, "yyyy-MM-dd"));
              }
              setOpen(false);
            }}
            classNames={{
              months: "flex flex-col gap-4",
              month: "space-y-2",
              caption: "flex justify-between items-center text-slate-200 text-sm",
              nav: "flex items-center gap-2",
              nav_button:
                "rounded-lg border border-night-500 bg-night-700 px-2 py-1 text-xs text-slate-200 hover:border-brand-600/60",
              table: "w-full border-collapse",
              head_row: "flex",
              head_cell: "w-8 text-[10px] uppercase tracking-[0.12em] text-slate-500",
              row: "flex w-full",
              cell: "w-8 h-8 flex items-center justify-center text-xs text-slate-200",
              day: "w-7 h-7 rounded-lg hover:bg-night-700",
              day_selected: "bg-brand-500 text-night-900",
              day_today: "border border-brand-600/60",
              day_outside: "text-slate-600",
            }}
          />
          <div className="mt-3 flex items-center justify-between">
            <button
              type="button"
              className="text-xs text-slate-400 hover:text-slate-200"
              onClick={() => onChange("")}
            >
              Clear
            </button>
            <button
              type="button"
              className="text-xs text-slate-400 hover:text-slate-200"
              onClick={() => setOpen(false)}
            >
              Close
            </button>
          </div>
        </Popover.Content>
      </Popover.Root>
    </div>
  );
}
