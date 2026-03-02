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
    <div className="mt-4">
      <PermissionGate action="enable" productId={permissionProductId}>
        <div className="flex flex-wrap items-end gap-2">
          {finding.active ? (
            <div className="w-56">
              <SelectField
                label="Close Action"
                value={reason}
                onChange={(value) => setReason(value as CloseReason)}
                options={reasonOptions}
              />
            </div>
          ) : null}
          {finding.active ? (
            <button
              className="h-10 rounded-xl bg-brand-500 px-4 text-xs font-semibold text-night-900 disabled:opacity-50"
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
              Apply
            </button>
          ) : (
            <button
              className="h-10 rounded-xl border border-brand-600/50 bg-transparent px-4 text-xs font-semibold text-brand-300 disabled:opacity-50"
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
