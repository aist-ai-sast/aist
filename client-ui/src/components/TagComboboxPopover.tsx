import type { KeyboardEvent, ReactNode } from "react";
import * as Popover from "@radix-ui/react-popover";
import { Command } from "cmdk";

type TagComboboxPopoverProps = {
  /** Accessible name of the command palette. */
  label: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The element that opens the popover; cloned by Radix, so it must forward refs. */
  trigger: ReactNode;
  query: string;
  onQueryChange: (query: string) => void;
  highlighted: string;
  onHighlightedChange: (value: string) => void;
  onKeyDown?: (event: KeyboardEvent<HTMLDivElement>) => void;
  searchPlaceholder?: string;
  /** Rendered between the search field and the list (match mode, validation error). */
  header?: ReactNode;
  /** Everything inside the list: options, empty state, groups. */
  children: ReactNode;
  footer?: ReactNode;
  contentClassName?: string;
};

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="rounded border border-night-500 px-1 font-mono text-[10px] text-slate-400">{children}</kbd>;
}

/**
 * The popover shell shared by every tag picker: Radix Popover for positioning,
 * dismiss and focus, cmdk for search and keyboard navigation.
 *
 * It owns no tag semantics at all. `TagFilter` fills it with tri-state
 * include/exclude rows, `EditableTagRow` with on/off rows plus a create row.
 */
export default function TagComboboxPopover({
  label,
  open,
  onOpenChange,
  trigger,
  query,
  onQueryChange,
  highlighted,
  onHighlightedChange,
  onKeyDown,
  searchPlaceholder = "Search tags…",
  header,
  children,
  footer,
  contentClassName,
}: TagComboboxPopoverProps) {
  return (
    <Popover.Root open={open} onOpenChange={onOpenChange}>
      <Popover.Trigger asChild>{trigger}</Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="start"
          sideOffset={8}
          className={[
            "z-[1700] overflow-hidden rounded-2xl border border-night-500 bg-night-900 shadow-panel",
            contentClassName ?? "w-[var(--radix-popover-trigger-width)] min-w-[280px]",
          ].join(" ")}
          onClick={(event) => event.stopPropagation()}
        >
          <Command label={label} value={highlighted} onValueChange={onHighlightedChange} onKeyDown={onKeyDown} loop>
            <div className="flex items-center gap-2 border-b border-night-500 px-3 py-2">
              <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 shrink-0 text-slate-400" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <circle cx="11" cy="11" r="7" />
                <path d="M21 21l-4.3-4.3" />
              </svg>
              <Command.Input
                value={query}
                onValueChange={onQueryChange}
                placeholder={searchPlaceholder}
                className="h-8 flex-1 bg-transparent text-sm text-white outline-none placeholder:text-slate-500"
              />
              <Kbd>Esc</Kbd>
            </div>
            {header}
            <Command.List className="max-h-64 overflow-y-auto p-1.5">{children}</Command.List>
            {footer}
          </Command>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
