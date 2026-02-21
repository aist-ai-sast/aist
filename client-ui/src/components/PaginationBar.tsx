import { useRef } from "react";

import SelectField from "./SelectField";
import TextInput from "./TextInput";
import { ACCENT_SELECTED_CLASS } from "../lib/uiClasses";

type PaginationBarProps = {
  count: number;
  pageIndex: number;
  pageSize: number;
  onPageIndexChange: (value: number) => void;
  onPageSizeChange: (value: number) => void;
  noun?: string;
  rowOptions?: number[];
};

export default function PaginationBar({
  count,
  pageIndex,
  pageSize,
  onPageIndexChange,
  onPageSizeChange,
  noun = "items",
  rowOptions = [25, 50, 100],
}: PaginationBarProps) {
  const totalPages = Math.max(1, Math.ceil(count / pageSize));
  const current = pageIndex + 1;
  const inputRef = useRef<HTMLInputElement | null>(null);
  const pages: Array<number | string> = [];
  const push = (value: number | string) => pages.push(value);

  push(1);
  const start = Math.max(2, current - 1);
  const end = Math.min(totalPages - 1, current + 1);
  if (start > 2) push("…");
  for (let i = start; i <= end; i += 1) push(i);
  if (end < totalPages - 1) push("…");
  if (totalPages > 1) push(totalPages);

  return (
    <div className="mt-2 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-night-500 bg-night-700 px-4 py-3 text-xs text-slate-300">
      <div>
        {count} {noun} · Page {current} of {totalPages}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <div className="w-28">
          <SelectField
            label="Rows"
            value={String(pageSize)}
            onChange={(value) => onPageSizeChange(Number(value))}
            hideLabel
            options={rowOptions.map((option) => ({
              value: String(option),
              label: `Rows: ${option}`,
            }))}
          />
        </div>
        <button
          className="rounded-xl border border-night-500 bg-night-800 px-3 py-2 text-xs text-slate-200 disabled:opacity-50"
          onClick={() => onPageIndexChange(Math.max(0, pageIndex - 1))}
          disabled={pageIndex === 0}
        >
          Prev
        </button>
        <div className="flex items-center gap-1">
          {pages.map((item, idx) =>
            typeof item === "string" ? (
              <span key={`ellipsis-${idx}`} className="px-2 text-slate-500">
                {item}
              </span>
            ) : (
              <button
                key={`page-${item}`}
                className={[
                  "min-w-8 rounded-lg border px-2 py-1 text-xs",
                  item === current
                    ? ACCENT_SELECTED_CLASS
                    : "border-night-500 bg-night-800 text-slate-200 hover:border-brand-500/40",
                ].join(" ")}
                onClick={() => onPageIndexChange(item - 1)}
                aria-current={item === current ? "page" : undefined}
              >
                {item}
              </button>
            ),
          )}
        </div>
        <button
          className="rounded-xl border border-night-500 bg-night-800 px-3 py-2 text-xs text-slate-200 disabled:opacity-50"
          onClick={() => onPageIndexChange(Math.min(totalPages - 1, pageIndex + 1))}
          disabled={current >= totalPages}
        >
          Next
        </button>
        <div className="flex items-center gap-2">
          <TextInput
            ref={inputRef}
            variant="compact"
            className="w-16"
            inputMode="numeric"
            placeholder="Page"
            onKeyDown={(event) => {
              if (event.key !== "Enter") return;
              const value = Number((event.target as HTMLInputElement).value);
              if (Number.isNaN(value)) return;
              const next = Math.min(Math.max(1, value), totalPages);
              onPageIndexChange(next - 1);
            }}
          />
          <button
            className="rounded-xl border border-night-500 bg-night-800 px-3 py-2 text-xs text-slate-200"
            onClick={() => {
              const value = Number(inputRef.current?.value ?? "");
              if (Number.isNaN(value)) return;
              const next = Math.min(Math.max(1, value), totalPages);
              onPageIndexChange(next - 1);
            }}
          >
            Go
          </button>
        </div>
      </div>
    </div>
  );
}
