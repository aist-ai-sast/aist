import { useState } from "react";

import type { Finding } from "../types";
import { useCloseFinding } from "../lib/mutations";
import { useToast } from "./ToastProvider";
import SelectField from "./SelectField";
import PermissionGate from "./PermissionGate";

type CloseReason = "mitigated" | "false_positive" | "out_of_scope" | "duplicate";

const reasonOptions: { value: CloseReason; label: string }[] = [
  { value: "mitigated", label: "Close as Mitigated" },
  { value: "false_positive", label: "Close as False Positive" },
  { value: "out_of_scope", label: "Close as Out of Scope" },
  { value: "duplicate", label: "Close as Duplicate" },
];

type FindingStatusActionsProps = {
  finding: Finding;
};

export default function FindingStatusActions({ finding }: FindingStatusActionsProps) {
  const toast = useToast();
  const closeFinding = useCloseFinding();
  const [reason, setReason] = useState<CloseReason>("mitigated");

  return (
    <div className="mt-4">
      <PermissionGate action="enable" productId={finding.productId}>
        <div className="flex flex-wrap items-end gap-2">
          <div className="w-56">
            <SelectField
              label="Close Action"
              value={reason}
              onChange={(value) => setReason(value as CloseReason)}
              options={reasonOptions}
            />
          </div>
          <button
            className="h-10 rounded-xl bg-brand-500 px-4 text-xs font-semibold text-night-900 disabled:opacity-50"
            onClick={() =>
              closeFinding.mutate(
                { id: finding.id, reason },
                {
                  onSuccess: () => {
                    toast.push("Finding closed.", "success");
                  },
                  onError: (error) => {
                    const message = error instanceof Error ? error.message : String(error);
                    toast.push(`Close failed: ${message}`, "error");
                  },
                },
              )
            }
            disabled={!finding.active || closeFinding.isPending}
          >
            Apply
          </button>
        </div>
      </PermissionGate>
    </div>
  );
}
