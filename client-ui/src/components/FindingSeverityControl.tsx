import { useEffect, useState } from "react";

import type { Finding } from "../types";
import { useUpdateFindingSeverity, type FindingSeverity } from "../lib/mutations";
import PermissionGate from "./PermissionGate";
import SelectField from "./SelectField";
import { useToast } from "./ToastProvider";

const severityOptions: Array<{ value: FindingSeverity; label: string }> = [
  { value: "Critical", label: "Critical" },
  { value: "High", label: "High" },
  { value: "Medium", label: "Medium" },
  { value: "Low", label: "Low" },
  { value: "Info", label: "Info" },
];

type Props = {
  finding: Finding;
  permissionProductId?: number;
  permissionOrganizationId?: number;
  onChanged?: (severity: FindingSeverity) => void;
  isLocked?: boolean;
};

export default function FindingSeverityControl({
  finding,
  permissionProductId,
  permissionOrganizationId,
  onChanged,
  isLocked = false,
}: Props) {
  const updateSeverity = useUpdateFindingSeverity();
  const [value, setValue] = useState<FindingSeverity>(finding.severity);
  const toast = useToast();
  useEffect(() => {
    setValue(finding.severity);
  }, [finding.severity]);

  return (
    <PermissionGate action="write" organizationId={permissionOrganizationId}>
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
              d="M4 6h8"
            />
            <path d="M14 6h6" />
            <circle cx="12" cy="6" r="2" />
            <path d="M4 12h3" />
            <path d="M11 12h9" />
            <circle cx="8" cy="12" r="2" />
            <path d="M4 18h10" />
            <path d="M18 18h2" />
            <circle cx="16" cy="18" r="2" />
          </svg>
          Severity
        </div>
        <SelectField
          label="Severity"
          value={value}
          onChange={(next) => {
            const normalized = next as FindingSeverity;
            if (normalized === value || updateSeverity.isPending || isLocked) return;
            setValue(normalized);
            updateSeverity.mutate(
              { id: finding.id, severity: normalized },
              {
                onSuccess: () => {
                  onChanged?.(normalized);
                  toast.push("Severity updated.", "success");
                },
                onError: (error) => {
                  setValue(finding.severity);
                  const message = error instanceof Error ? error.message : String(error);
                  toast.push(`Severity update failed: ${message}`, "error");
                },
              },
            );
          }}
          options={severityOptions}
          disabled={updateSeverity.isPending || isLocked}
          hideLabel
        />
      </div>
    </PermissionGate>
  );
}
