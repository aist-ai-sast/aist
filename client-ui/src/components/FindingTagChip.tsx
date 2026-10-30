import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import TagChip from "./TagChip";
import type { TagFilterState } from "../lib/tagFilter";

type FindingTagChipProps = {
  tag: string;
  state: TagFilterState;
  onInclude: () => void;
  onExclude: () => void;
  onOnly: () => void;
  /** Present only where the viewer may edit the finding's tags. */
  onRemove?: () => void;
  /** The tag is not the finding's own: it can be filtered on, not removed. */
  locked?: boolean;
  lockedReason?: string;
};

const MENU_ITEM_CLASS =
  "flex cursor-pointer select-none items-center gap-2 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none data-[highlighted]:bg-night-700 data-[highlighted]:text-white";

/**
 * A finding's tag as shown in its detail view. Clicking opens a small menu that
 * feeds the page-level tag filter: include, exclude, or keep only this tag —
 * and, for a viewer who may edit the finding, removes the tag.
 *
 * Removal lives in this menu rather than on a × of its own: the chip is already
 * a menu trigger, and a button inside a button is not valid markup.
 */
export default function FindingTagChip({
  tag,
  state,
  onInclude,
  onExclude,
  onOnly,
  onRemove,
  locked,
  lockedReason,
}: FindingTagChipProps) {
  return (
    <DropdownMenu.Root modal={false}>
      <DropdownMenu.Trigger asChild>
        <TagChip
          as="button"
          tag={tag}
          size="md"
          aria-label={`Filter by tag ${tag}`}
          locked={locked}
          title={locked ? lockedReason : undefined}
          variant={state === "include" ? "include" : state === "exclude" ? "exclude" : "neutral"}
          className={state === "none" ? "hover:border-brand-600/40" : ""}
        />
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="start"
          sideOffset={6}
          className="z-[1700] min-w-[200px] rounded-xl border border-night-500 bg-night-900 p-1 shadow-panel"
        >
          <DropdownMenu.Label className="px-3 py-1.5 font-mono text-[11px] text-slate-400">{tag}</DropdownMenu.Label>
          <DropdownMenu.Item className={MENU_ITEM_CLASS} onSelect={onInclude}>
            <span aria-hidden="true" className="h-2 w-2 rounded-full bg-brand-500" />
            {state === "include" ? "Stop including" : "Include tag"}
          </DropdownMenu.Item>
          <DropdownMenu.Item className={MENU_ITEM_CLASS} onSelect={onExclude}>
            <span aria-hidden="true" className="h-2 w-2 rounded-full bg-danger-500" />
            {state === "exclude" ? "Stop excluding" : "Exclude tag"}
          </DropdownMenu.Item>
          <DropdownMenu.Item className={MENU_ITEM_CLASS} onSelect={onOnly}>
            <span aria-hidden="true" className="h-2 w-2 rounded-full bg-slate-400" />
            Only this tag
          </DropdownMenu.Item>
          {onRemove ? (
            <>
              <DropdownMenu.Separator className="my-1 h-px bg-night-500" />
              <DropdownMenu.Item className={MENU_ITEM_CLASS} onSelect={onRemove}>
                <span aria-hidden="true" className="h-2 w-2 rounded-full border border-slate-400" />
                Remove tag
              </DropdownMenu.Item>
            </>
          ) : null}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
