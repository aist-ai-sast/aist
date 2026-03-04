import { useState } from "react";
import * as Popover from "@radix-ui/react-popover";
import { useCweMeta } from "../lib/queries";

type CweTooltipProps = {
  cwe: number | null | undefined;
  /** Custom trigger content; defaults to "CWE-N". */
  children?: React.ReactNode;
  className?: string;
};

/**
 * Displays "CWE-N" with a hover popup showing full name, description and impact.
 * Uses Radix Popover.Anchor (no click toggle) so open state is controlled purely
 * by onMouseEnter/Leave on the trigger span.
 * Content has pointer-events:none — it's display-only, no interactive gap to cross.
 */
export default function CweTooltip({ cwe, children, className }: CweTooltipProps) {
  const metaQuery = useCweMeta(cwe);
  const meta = metaQuery.data;
  const [open, setOpen] = useState(false);

  if (!cwe) return null;

  const label = children ?? `CWE-${cwe}`;

  if (!meta) {
    return (
      <span className={className} title={`CWE-${cwe}`}>
        {label}
      </span>
    );
  }

  return (
    <Popover.Root open={open}>
      <Popover.Anchor asChild>
        <span
          className={["inline-block cursor-default underline decoration-dotted decoration-slate-500 underline-offset-2", className ?? ""].join(" ").trim()}
          onMouseEnter={() => setOpen(true)}
          onMouseLeave={() => setOpen(false)}
        >
          {label}
        </span>
      </Popover.Anchor>
      <Popover.Portal>
        <Popover.Content
          side="bottom"
          align="start"
          sideOffset={6}
          onOpenAutoFocus={(e) => e.preventDefault()}
          style={{
            pointerEvents: "none",
            width: "min(360px, 88vw)",
            maxHeight: "260px",
            overflowY: "auto",
            backgroundColor: "rgba(15,23,42,0.97)",
            border: "1px solid rgba(45,67,105,0.85)",
            borderRadius: "0.75rem",
            padding: "0.75rem 1rem",
            color: "#e2e8f0",
            fontSize: "12px",
            lineHeight: "1.5",
            textAlign: "left",
            wordBreak: "break-word",
            overflowWrap: "anywhere",
            boxShadow: "0 4px 24px rgba(0,0,0,0.5)",
            zIndex: 9999,
          }}
        >
          <p style={{ margin: 0, fontWeight: 600, color: "#e5f3ff" }}>
            CWE-{cwe}: {meta.title}
          </p>
          {meta.description ? (
            <p style={{ margin: "0.375rem 0 0", fontSize: "11px", color: "#94a3b8" }}>
              {meta.description}
            </p>
          ) : null}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
