import { useState } from "react";
import * as Popover from "@radix-ui/react-popover";

import type { Finding } from "../types";
import { useCloseFinding, useUpdateFindingStatus, type FindingCloseReason } from "../lib/mutations";
import { findingStatusBadgeClass } from "../lib/badgeStyles";
import { useToast } from "./ToastProvider";
import SelectField from "./SelectField";
import PermissionGate from "./PermissionGate";

const reasonOptions: { value: FindingCloseReason; label: string }[] = [
  { value: "mitigated", label: "Mitigated (Fixed)" },
  { value: "false_positive", label: "False Positive" },
  { value: "out_of_scope", label: "Out Of Scope" },
  { value: "duplicate", label: "Duplicate" },
];

const statusHelpRows = [
  {
    status: "Under Review",
    semantics: "Triage is still in progress, and there is no final decision yet.",
    retest: "After retest, it may stay under review, move to a closing status, or reappear as a new active finding.",
  },
  {
    status: "Active",
    semantics: "The finding is open and requires action.",
    retest: "A retest can keep it active, update it, or create a new active record when matching rules do not link it.",
  },
  {
    status: "Inactive",
    semantics: "The finding is closed in the current workflow.",
    retest: "It may stay inactive, or a new active finding can appear if the issue is detected again as a separate record.",
  },
  {
    status: "Verified",
    semantics: "The finding was validated as real by reviewer or process.",
    retest: "Verification is a confidence marker and is not automatically removed from historical records.",
  },
  {
    status: "Mitigated",
    semantics: "The issue is considered fixed in code or configuration.",
    retest: "If detected again, it is usually treated as regression and appears as a new active finding.",
  },
  {
    status: "False Positive",
    semantics: "The result is considered non-exploitable or incorrect.",
    retest: "Matching often keeps it effectively closed, but unmatched imports can still create a new active finding.",
  },
  {
    status: "Out of Scope",
    semantics: "The finding is valid but outside agreed remediation scope.",
    retest: "This decision is historical; if matching fails, a new active finding can still appear.",
  },
  {
    status: "Duplicate",
    semantics: "The finding is linked to another root finding and not triaged independently.",
    retest: "The duplicate record is typically closed and removed from independent triage; unmatched future detections may still create a new active finding.",
  },
] as const;

type FindingStatusActionsProps = {
  finding: Finding;
  permissionProductId?: number;
  onApplied?: (reason: FindingCloseReason) => void;
  onReopened?: () => void;
  isLocked?: boolean;
};

export default function FindingStatusActions({
  finding,
  permissionProductId,
  onApplied,
  onReopened,
  isLocked = false,
}: FindingStatusActionsProps) {
  const toast = useToast();
  const closeFinding = useCloseFinding();
  const updateFindingStatus = useUpdateFindingStatus();
  const [reason, setReason] = useState<FindingCloseReason>("mitigated");

  return (
    <div>
      <PermissionGate action="enable" productId={permissionProductId}>
        <div className="flex flex-wrap items-end gap-2">
          {finding.active ? (
            <div className="w-56">
              <div className="mb-1 flex items-center gap-1 text-xs text-slate-400">
                <svg
                  viewBox="0 0 24 24"
                  className="h-3.5 w-3.5"
                  aria-hidden="true"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path
                    d="m9 12 2 2 4-4"
                  />
                  <circle cx="12" cy="12" r="8" />
                </svg>
                Close Action
                <Popover.Root>
                  <Popover.Trigger asChild>
                    <button
                      type="button"
                      className="inline-flex h-5 w-5 items-center justify-center rounded-full border border-night-500 bg-night-800 text-slate-300 transition hover:border-brand-600/50 hover:text-brand-200"
                      aria-label="Status help"
                      title="Status help"
                    >
                      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
                        <path
                          fill="currentColor"
                          d="M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2Zm0 15.2a1.2 1.2 0 1 1 1.2-1.2 1.2 1.2 0 0 1-1.2 1.2Zm1.7-5.8-.5.3a1.4 1.4 0 0 0-.7 1.2v.4h-1.8v-.4a3.2 3.2 0 0 1 1.6-2.8l.5-.3a1.6 1.6 0 1 0-2.4-1.4H8.6a3.4 3.4 0 1 1 5.1 3Z"
                        />
                      </svg>
                    </button>
                  </Popover.Trigger>
                  <Popover.Portal>
                    <Popover.Content
                      side="bottom"
                      sideOffset={10}
                      align="start"
                      avoidCollisions
                      collisionPadding={{ top: 16, right: 12, bottom: 16, left: 12 }}
                      className="z-[1200] flex w-[min(680px,calc(100vw-1rem))] flex-col rounded-2xl border border-night-500 bg-night-900 p-4 shadow-panel"
                      style={{ maxHeight: "var(--radix-popover-content-available-height)" }}
                    >
                      <div className="mb-3 flex-shrink-0 border-b border-night-500 pb-2">
                        <div className="text-sm font-semibold text-slate-100">Status Guide</div>
                        <div className="mt-1 text-xs text-slate-400">Finding workflow meanings and expected behavior during retest.</div>
                      </div>
                      <div className="aist-scrollbar min-h-0 flex-1 space-y-2 overflow-y-auto pr-1">
                        {statusHelpRows.map((row) => (
                          <div key={row.status} className="rounded-xl border border-night-500 bg-night-800/70 p-3">
                            <div className="grid gap-2 sm:grid-cols-[170px_1fr] sm:items-start">
                              <div>
                                <span className={`inline-flex rounded-full border px-2.5 py-1 text-[11px] font-semibold tracking-wide ${findingStatusBadgeClass(row.status)}`}>
                                  {row.status}
                                </span>
                              </div>
                              <div className="space-y-1.5">
                                <div>
                                  <div className="text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-400">Meaning</div>
                                  <div className="break-words text-xs leading-5 text-slate-200">{row.semantics}</div>
                                </div>
                                <div>
                                  <div className="text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-400">On Retest</div>
                                  <div className="break-words text-xs leading-5 text-slate-300">{row.retest}</div>
                                </div>
                              </div>
                            </div>
                          </div>
                        ))}
                      </div>
                    </Popover.Content>
                  </Popover.Portal>
                </Popover.Root>
              </div>
              <SelectField
                label="Close Action"
                value={reason}
                onChange={(value) => setReason(value as FindingCloseReason)}
                options={reasonOptions}
                hideLabel
              />
            </div>
          ) : null}
          {finding.active ? (
            <button
              className="inline-flex h-10 items-center gap-1.5 rounded-xl bg-brand-500 pl-3 pr-4 text-xs font-semibold text-night-900 disabled:opacity-50"
              onClick={() =>
                closeFinding.mutate(
                  { id: finding.id, reason },
                  {
                    onSuccess: () => {
                      onApplied?.(reason);
                      toast.push("Finding closed.", "success");
                    },
                    onError: (error) => {
                      const message = error instanceof Error ? error.message : String(error);
                      toast.push(`Close failed: ${message}`, "error");
                    },
                  },
                )
              }
              disabled={closeFinding.isPending || isLocked}
            >
              <svg
                viewBox="0 0 24 24"
                className="h-4 w-4"
                aria-hidden="true"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path
                  d="m5 12 4 4L19 6"
                />
              </svg>
              Apply
            </button>
          ) : (
            <button
              className="inline-flex h-10 items-center gap-2 rounded-xl border border-brand-600/50 bg-transparent px-4 text-xs font-semibold text-brand-300 disabled:opacity-50"
              onClick={() =>
                updateFindingStatus.mutate(
                  { id: finding.id, active: true, clearCloseFlags: true },
                  {
                    onSuccess: () => {
                      onReopened?.();
                      toast.push("Finding reopened.", "success");
                    },
                    onError: (error) => {
                      const message = error instanceof Error ? error.message : String(error);
                      toast.push(`Reopen failed: ${message}`, "error");
                    },
                  },
                )
              }
              disabled={updateFindingStatus.isPending || isLocked}
            >
              <svg
                viewBox="0 0 24 24"
                className="h-4 w-4"
                aria-hidden="true"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.9"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path
                  d="M3 12a9 9 0 1 0 3-6.7"
                />
                <path d="M3 4v4h4" />
              </svg>
              Reopen
            </button>
          )}
          {isLocked ? (
            <div className="text-xs text-amber-300">Locked by active bulk update.</div>
          ) : null}
        </div>
      </PermissionGate>
    </div>
  );
}
