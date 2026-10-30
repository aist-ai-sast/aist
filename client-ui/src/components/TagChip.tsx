import { forwardRef, type ReactNode } from "react";

import { ACCENT_SELECTED_CLASS } from "../lib/uiClasses";

export type TagChipVariant = "neutral" | "include" | "exclude" | "locked";

type TagChipProps = {
  tag: string;
  variant?: TagChipVariant;
  /** `sm` is the density used on cards, `md` the one used in detail panels. */
  size?: "sm" | "md";
  title?: string;
  /** Draws the lock without claiming the chip's colour, which a variant may already use. */
  locked?: boolean;
  /** Renders the × inside the chip. Only for a chip that is not itself a button. */
  onRemove?: () => void;
  removeLabel?: string;
  /** `button` makes the chip usable as a Radix trigger; it then takes no `onRemove`. */
  as?: "span" | "button";
  className?: string;
} & Omit<React.HTMLAttributes<HTMLElement>, "title" | "className">;

const VARIANT_CLASS: Record<TagChipVariant, string> = {
  neutral: "border-night-500 bg-night-800 text-slate-300",
  include: ACCENT_SELECTED_CLASS,
  exclude: "border-danger-500/50 bg-danger-500/10 text-danger-500",
  locked: "border-night-500 bg-night-900 font-mono text-slate-500",
};

/**
 * The one tag chip of the product. Every tag rendered anywhere — a filter
 * selection, a finding's tag, a project's tag — is this component, so they
 * share one height, one padding and one set of state colours.
 *
 * Fixed height plus `leading-none` is deliberate: a row mixing proportional,
 * monospace and icon-bearing chips lines up only if the height does not follow
 * the glyphs.
 */
const TagChip = forwardRef<HTMLElement, TagChipProps>(function TagChip(
  { tag, variant = "neutral", size = "sm", title, locked, onRemove, removeLabel, as = "span", className, ...rest },
  ref,
) {
  const excluded = variant === "exclude";
  const sizeClass = size === "md" ? "h-[26px] text-xs" : "h-[22px] text-[11px]";
  const paddingClass = onRemove ? "pl-2.5 pr-1" : "px-2.5";
  const chipClass = [
    "inline-flex shrink-0 items-center gap-1 rounded-full border leading-none whitespace-nowrap outline-none transition",
    sizeClass,
    paddingClass,
    VARIANT_CLASS[variant],
    as === "button"
      ? "focus-visible:ring-2 focus-visible:ring-brand-600/60 data-[state=open]:ring-2 data-[state=open]:ring-brand-600/60"
      : "",
    className ?? "",
  ]
    .filter(Boolean)
    .join(" ");

  const body: ReactNode = (
    <>
      {variant === "locked" || locked ? <LockIcon /> : null}
      {excluded ? (
        <span className="font-sans text-[9px] uppercase leading-none tracking-[0.1em] opacity-80">not</span>
      ) : null}
      <span className={["block leading-none", excluded ? "line-through" : ""].join(" ")}>{tag}</span>
    </>
  );

  if (as === "button") {
    return (
      <button
        type="button"
        ref={ref as React.Ref<HTMLButtonElement>}
        title={title}
        className={chipClass}
        {...rest}
      >
        {body}
      </button>
    );
  }

  return (
    <span ref={ref as React.Ref<HTMLSpanElement>} title={title} className={chipClass} {...rest}>
      {body}
      {onRemove ? (
        <button
          type="button"
          aria-label={removeLabel ?? `Remove ${tag}`}
          className="tag-chip-remove grid h-4 w-4 shrink-0 place-items-center rounded-full text-current transition hover:bg-white/10"
          onClick={(event) => {
            event.stopPropagation();
            onRemove();
          }}
        >
          <CloseIcon />
        </button>
      ) : null}
    </span>
  );
});

function CloseIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-2.5 w-2.5" fill="none" stroke="currentColor" strokeWidth="3.2" strokeLinecap="round" aria-hidden="true">
      <path d="M6 6l12 12M18 6L6 18" />
    </svg>
  );
}

function LockIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-2.5 w-2.5 shrink-0" fill="none" stroke="currentColor" strokeWidth="2.2" aria-hidden="true">
      <rect x="5" y="11" width="14" height="9" rx="2" />
      <path d="M8 11V8a4 4 0 0 1 8 0v3" />
    </svg>
  );
}

export default TagChip;
