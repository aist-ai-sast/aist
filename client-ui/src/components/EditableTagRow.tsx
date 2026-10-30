import { useMemo, useState, type KeyboardEvent, type ReactNode } from "react";
import { Command } from "cmdk";

import PermissionGate from "./PermissionGate";
import TagChip from "./TagChip";
import TagComboboxPopover, { Kbd } from "./TagComboboxPopover";
import type { PermissionAction } from "../lib/permissions";
import { isSystemTag, validateTag } from "../lib/tagRules";

export type EditableTagRowProps = {
  tags: string[];
  /** Tags already used elsewhere in scope; offered in the picker. */
  suggestions: string[];
  /** The subset of `tags` inherited from the project: shown, never editable here. */
  inheritedTags?: string[];
  /** The capability that editing this object's tags requires. */
  action: PermissionAction;
  /**
   * Organization that owns the object. Until it is known the row stays
   * read-only: without it the role falls back to the best one the user holds
   * anywhere, which would offer the control on an object the server refuses.
   */
  permissionOrganizationId?: number;
  /** Replaces the whole editable tag list. System tags are never included. */
  onChange: (tags: string[]) => void;
  /**
   * A write is in flight. The row dims and locks: the next edit must start
   * from the list the server confirmed, never from the one being replaced.
   */
  pending?: boolean;
  /** Tags shown before the "+N" button collapses the rest. 0 shows all. */
  visibleCount?: number;
  size?: "sm" | "md";
  /** Names the owning object in the controls' labels, e.g. "project billing-gateway". */
  ownerLabel?: string;
  counts?: Record<string, number>;
  /** Shown in the picker while it is open. */
  footnote?: string;
  /** Replaces `footnote`, in alert style, while the draft drops tags. */
  removalFootnote?: (removed: string[]) => string;
  /**
   * Replaces the default chip. Used where a tag carries an extra action — on
   * the findings list the chip also opens the include/exclude filter menu.
   */
  renderTag?: (
    tag: string,
    controls: { locked: boolean; lockedReason?: string; onRemove?: () => void },
  ) => ReactNode;
};

type TagKind = "own" | "project" | "system";

/** Shown when a locked tag is picked. */
const UNEDITABLE_REASON: Record<Exclude<TagKind, "own">, string> = {
  project: "This tag comes from the project. Edit it on the project card.",
  system: "Platform tags cannot be changed.",
};

const CHIP_TITLE: Record<Exclude<TagKind, "own">, string> = {
  project: "From project — managed on the project card.",
  system: "Set by the platform",
};

/** Own tags first, then the project's, then the platform's. */
const KIND_ORDER: Record<TagKind, number> = { own: 0, project: 1, system: 2 };

const GROUP_HEADING_CLASS =
  "[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:text-[10px] [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-[0.12em] [&_[cmdk-group-heading]]:text-slate-500";

const ADD_BUTTON_BASE =
  "tag-add-button grid place-items-center rounded-full border border-dashed outline-none transition focus-visible:ring-2 focus-visible:ring-brand-600/60";

/**
 * A row of tags that can be edited in place.
 *
 * It knows nothing about projects or findings — the caller supplies the tags,
 * the vocabulary and the capability to check, and receives the full new list.
 * The capability itself is resolved by the shared `PermissionGate`. Edits are
 * collected while the picker is open and handed over once, on close, so one
 * editing session is one request.
 */
export default function EditableTagRow({
  tags,
  suggestions,
  action,
  permissionOrganizationId,
  onChange,
  pending = false,
  visibleCount = 0,
  size = "sm",
  ownerLabel,
  counts,
  footnote,
  removalFootnote,
  inheritedTags,
  renderTag,
}: EditableTagRowProps) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<string[] | null>(null);
  const [query, setQuery] = useState("");
  const [highlighted, setHighlighted] = useState("");
  const [error, setError] = useState("");

  // While the picker is open the row shows the draft, so chips react instantly.
  const shown = draft ?? tags;
  const ownerSuffix = ownerLabel ? ` to ${ownerLabel}` : "";

  // A tag is editable only on the object that owns it.
  const inherited = useMemo(() => new Set(inheritedTags ?? []), [inheritedTags]);
  const kindOf = (tag: string): TagKind =>
    isSystemTag(tag) ? "system" : inherited.has(tag) ? "project" : "own";
  const isOwn = (tag: string) => kindOf(tag) === "own";
  const editableTags = shown.filter(isOwn);

  const options = useMemo(() => {
    const all = [...new Set([...suggestions, ...shown])];
    const rank = (tag: string) => (shown.includes(tag) ? 0 : 1);
    return all.sort((left, right) => rank(left) - rank(right) || left.localeCompare(right));
  }, [suggestions, shown]);

  const regularOptions = options.filter(isOwn);
  const projectOptions = options.filter((tag) => kindOf(tag) === "project");
  const systemOptions = options.filter(isSystemTag);
  const normalizedQuery = query.trim().toLowerCase();
  const matches = (tag: string) => !normalizedQuery || tag.toLowerCase().includes(normalizedQuery);
  // The cap counts only tags the caller can remove.
  const creatable = validateTag(query, editableTags);
  const canCreate = Boolean(
    query.trim() && !creatable.error && creatable.tag && !options.includes(creatable.tag),
  );

  const commit = (next: string[]) => {
    setDraft(next);
    setError("");
  };

  const applyAndClose = (nextOpen: boolean) => {
    setOpen(nextOpen);
    if (nextOpen) {
      setDraft(tags);
      setQuery("");
      setHighlighted("");
      setError("");
      return;
    }
    const next = draft;
    setDraft(null);
    setQuery("");
    setError("");
    if (next && next.join("\u0000") !== tags.join("\u0000")) {
      onChange(next.filter(isOwn));
    }
  };

  const toggle = (tag: string) => {
    const kind = kindOf(tag);
    if (kind !== "own") {
      setError(UNEDITABLE_REASON[kind]);
      return;
    }
    commit(shown.includes(tag) ? shown.filter((item) => item !== tag) : [...shown, tag]);
  };

  const create = () => {
    const { tag, error: reason } = validateTag(query, editableTags);
    if (reason || !tag) {
      setError(reason ?? "Enter a tag name.");
      return;
    }
    if (!shown.includes(tag)) {
      commit([...shown, tag]);
    }
    setQuery("");
  };

  const removeDirectly = (tag: string) => {
    onChange(tags.filter((item) => item !== tag && isOwn(item)));
  };

  // cmdk owns Enter (→ onSelect); Backspace on an empty search drops the last tag.
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Backspace" && !query) {
      const removable = editableTags;
      if (!removable.length) return;
      event.preventDefault();
      commit(shown.filter((tag) => tag !== removable[removable.length - 1]));
    }
  };

  // Display order only; `draft` keeps insertion order, which the close comparison relies on.
  const ordered = useMemo(
    () => [...shown].sort((left, right) => KIND_ORDER[kindOf(left)] - KIND_ORDER[kindOf(right)]),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [shown, inherited],
  );
  const visible = visibleCount > 0 ? ordered.slice(0, visibleCount) : ordered;
  const overflow = ordered.length - visible.length;

  const removedInDraft = draft ? tags.filter((tag) => isOwn(tag) && !draft.includes(tag)) : [];
  const removalWarning = removalFootnote && removedInDraft.length ? removalFootnote(removedInDraft) : "";

  const addButton = (
    <button
      type="button"
      aria-label={`Add tag${ownerSuffix}`}
      aria-expanded={open}
      title="Add tag"
      className={[
        ADD_BUTTON_BASE,
        shown.length === 0
          ? `gap-1 ${size === "md" ? "h-[26px] px-2.5 text-xs" : "h-[22px] px-2.5 text-[11px]"}`
          : size === "md"
            ? "h-[26px] w-[26px]"
            : "h-[22px] w-[22px]",
        open ? "border-brand-500/60 text-brand-500" : "border-night-500 text-slate-400 hover:border-brand-500/60 hover:text-brand-500",
      ].join(" ")}
    >
      <PlusIcon />
      {shown.length === 0 ? <span className="block leading-none">Add tag</span> : null}
    </button>
  );

  const renderOption = (tag: string) => {
    const selected = shown.includes(tag);
    const kind = kindOf(tag);
    const locked = kind !== "own";
    return (
      <Command.Item
        key={tag}
        value={tag}
        onSelect={() => toggle(tag)}
        className={[
          "flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-sm outline-none data-[selected=true]:bg-night-700",
          locked ? "cursor-not-allowed text-slate-500" : selected ? "text-brand-100" : "text-slate-200",
        ].join(" ")}
      >
        <span
          aria-hidden="true"
          className={[
            "grid h-4 w-4 shrink-0 place-items-center rounded border",
            selected
              ? locked
                ? "border-slate-600 bg-slate-600 text-night-900"
                : "border-brand-500 bg-brand-500 text-night-900"
              : "border-slate-500",
          ].join(" ")}
        >
          {selected ? <CheckIcon /> : null}
        </span>
        <span className="flex-1 truncate font-mono text-xs leading-none">{tag}</span>
        {locked ? (
          <span className="shrink-0 text-[10px] uppercase leading-none tracking-[0.1em] text-slate-600">
            {kind === "project" ? "project" : "system"}
          </span>
        ) : counts?.[tag] !== undefined ? (
          <span className="shrink-0 font-mono text-[11px] leading-none tabular-nums text-slate-500">{counts[tag]}</span>
        ) : null}
      </Command.Item>
    );
  };

  /**
   * One row, rendered twice by the gate below: once with the controls for a
   * viewer who may edit, once without for everyone else. `pending` locks the
   * controls of the editable one — that is a write in flight, not a permission.
   */
  const row = (editable: boolean) => (
    <div className={["flex flex-wrap items-center gap-1.5", pending ? "opacity-55" : ""].join(" ")}>
      {visible.map((tag) => {
        const kind = kindOf(tag);
        const locked = kind !== "own";
        const lockedReason = kind === "own" ? undefined : CHIP_TITLE[kind];
        const onRemove = editable && !pending && !locked && !open ? () => removeDirectly(tag) : undefined;
        if (renderTag) {
          return <span key={tag}>{renderTag(tag, { locked, lockedReason, onRemove })}</span>;
        }
        return (
          <TagChip
            key={tag}
            tag={tag}
            size={size}
            variant={locked ? "locked" : "neutral"}
            title={lockedReason}
            onRemove={onRemove}
            removeLabel={`Remove tag ${tag}${ownerSuffix}`}
          />
        );
      })}

      {overflow > 0 ? (
        <TagChip
          tag={`+${overflow}`}
          size={size}
          className="text-slate-400"
          title={ordered.slice(visible.length).join(", ")}
        />
      ) : null}

      {editable && !pending ? (
        <TagComboboxPopover
          label={`Tags${ownerSuffix}`}
          open={open}
          onOpenChange={applyAndClose}
          trigger={addButton}
          query={query}
          onQueryChange={(value) => {
            setQuery(value);
            setError("");
          }}
          highlighted={highlighted}
          onHighlightedChange={setHighlighted}
          onKeyDown={handleKeyDown}
          searchPlaceholder="Find or create a tag…"
          contentClassName="w-[300px] max-w-[calc(100vw-32px)]"
          header={
            error || removalWarning ? (
              <div role="alert" className="border-b border-night-500 bg-danger-500/10 px-3 py-1.5 text-[11px] leading-snug text-danger-500">
                {error || removalWarning}
              </div>
            ) : null
          }
          footer={
            <>
              {footnote ? (
                <p className="m-0 flex items-start gap-1.5 border-t border-night-500 bg-night-800/70 px-3 py-2 text-[11px] leading-snug text-amber-400">
                  <WarningIcon />
                  {footnote}
                </p>
              ) : null}
              <div className="flex items-center justify-between gap-2 border-t border-night-500 px-3 py-2 text-[11px] text-slate-500">
                <span className="flex items-center gap-1">
                  <Kbd>↵</Kbd> select <Kbd>⌫</Kbd> remove last
                </span>
                <button
                  type="button"
                  className="text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-300 transition hover:text-brand-300"
                  onClick={() => applyAndClose(false)}
                >
                  Done
                </button>
              </div>
            </>
          }
        >
          {canCreate ? (
            <Command.Item
              // cmdk scores every item against the query; the create row must not
              // be scored away by the very text that produced it.
              forceMount
              value={`create-${creatable.tag}`}
              onSelect={create}
              className="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-sm text-brand-100 outline-none data-[selected=true]:bg-night-700"
            >
              <span aria-hidden="true" className="grid h-4 w-4 shrink-0 place-items-center rounded border border-brand-500 text-brand-500">
                <PlusIcon />
              </span>
              <span className="flex-1 truncate text-xs leading-none">
                Create <span className="font-mono">{creatable.tag}</span>
              </span>
            </Command.Item>
          ) : null}

          {options.length === 0 ? (
            <div className="px-3 py-4 text-center text-xs text-slate-500">No tags yet — type to create one.</div>
          ) : (
            <Command.Empty className="px-3 py-4 text-center text-xs text-slate-500">
              {query.trim() && !canCreate ? `No tags match “${query}”` : null}
            </Command.Empty>
          )}

          {regularOptions.filter(matches).map(renderOption)}

          {projectOptions.filter(matches).length > 0 ? (
            <Command.Group
              heading="From project"
              className={GROUP_HEADING_CLASS}
            >
              {projectOptions.filter(matches).map(renderOption)}
            </Command.Group>
          ) : null}

          {systemOptions.filter(matches).length > 0 ? (
            <Command.Group heading="System" className={GROUP_HEADING_CLASS}>
              {systemOptions.filter(matches).map(renderOption)}
            </Command.Group>
          ) : null}
        </TagComboboxPopover>
      ) : null}

      {pending ? (
        <span role="status" className="text-[11px] leading-none text-slate-500">
          Saving…
        </span>
      ) : null}
    </div>
  );

  // The organization has to be known before the gate can resolve the role; while
  // it is not, the read-only row is what the viewer gets.
  if (permissionOrganizationId === undefined) {
    return row(false);
  }

  return (
    <PermissionGate
      action={action}
      organizationId={permissionOrganizationId}
      fallback={row(false)}
      loadingFallback={row(false)}
    >
      {row(true)}
    </PermissionGate>
  );
}

function PlusIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" aria-hidden="true">
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

function WarningIcon() {
  return (
    <svg viewBox="0 0 24 24" className="mt-[1px] h-3 w-3 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <path d="M12 9v4M12 17h.01" />
      <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-2.5 w-2.5" fill="none" stroke="currentColor" strokeWidth="3.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 13l4 4L19 7" />
    </svg>
  );
}

