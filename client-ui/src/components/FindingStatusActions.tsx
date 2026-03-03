import { useState } from "react";

import type { Finding } from "../types";
import { useCloseFinding, useUpdateFindingStatus, type FindingCloseReason } from "../lib/mutations";
import { useToast } from "./ToastProvider";
import SelectField from "./SelectField";
import PermissionGate from "./PermissionGate";

const reasonOptions: { value: FindingCloseReason; label: string }[] = [
  { value: "mitigated", label: "Close as Mitigated" },
  { value: "false_positive", label: "Close as False Positive" },
  { value: "out_of_scope", label: "Close as Out of Scope" },
  { value: "duplicate", label: "Close as Duplicate" },
];

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
              className="inline-flex h-10 items-center gap-2 rounded-xl bg-brand-500 px-4 text-xs font-semibold text-night-900 disabled:opacity-50"
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
