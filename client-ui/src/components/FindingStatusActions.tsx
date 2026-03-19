import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import * as Popover from "@radix-ui/react-popover";

import type { Finding } from "../types";
import {
  useCloseFinding,
  useRevokeRiskApproval,
  useRiskApproveFinding,
  useUpdateFindingStatus,
  type FindingCloseReason,
} from "../lib/mutations";
import { useRiskApprovalStatus } from "../lib/queries";
import { findingStatusBadgeClass } from "../lib/badgeStyles";
import { useToast } from "./ToastProvider";
import SelectField from "./SelectField";
import PermissionGate from "./PermissionGate";
import TextInput from "./TextInput";
import DateField from "./DateField";
import { formatDateForUI } from "../lib/dateDisplay";

const JUSTIFICATION_MAX_LENGTH = 4096;

const reasonOptions: { value: FindingCloseReason; label: string }[] = [
  { value: "mitigated", label: "Mitigated (Fixed)" },
  { value: "false_positive", label: "False Positive" },
  { value: "out_of_scope", label: "Out Of Scope" },
  { value: "duplicate", label: "Duplicate" },
];

const statusHelpRows = [
  {
    status: "Mitigated",
    semantics: "The issue is considered fixed (for example, code changed or unsafe secret removed).",
    retest:
      "If the scanner finds the same issue again and deduplication matches this record, the existing finding is updated. If matching fails, a new active finding is created — treating it as a regression.",
  },
  {
    status: "False Positive",
    semantics: "The team decided the scanner result is not a real vulnerability in this context.",
    retest:
      "Matched re-detections are deduplicated to this record and stay closed. If the import creates an unmatched record it will appear as a new active finding — requiring another triage decision.",
  },
  {
    status: "Out of Scope",
    semantics: "The issue is valid, but remediation is outside the agreed ownership or scope.",
    retest:
      "This decision stays on the historical record. Matched re-imports are deduplicated and stay closed; unmatched detections create a new active finding.",
  },
  {
    status: "Duplicate",
    semantics: "This record is tracked under another primary finding and is not triaged separately.",
    retest:
      "The duplicate stays closed and linked to the primary finding. The primary finding is the one to act on.",
  },
  {
    status: "Risk Accepted",
    semantics:
      "The issue is acknowledged as real but the business has formally accepted the risk with a documented justification. The finding becomes inactive while the approval is in effect.",
    retest:
      "Matched re-detections are deduplicated to the same record and remain risk-accepted. If the approval has an expiration date and the 'reopen on expiry' option is set, the finding automatically becomes active again once the approval expires.",
  },
] as const;

type FindingStatusActionsProps = {
  finding: Finding;
  permissionProductId?: number;
  onApplied?: (reason: FindingCloseReason | "risk_accepted") => void;
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
  const riskApproveFinding = useRiskApproveFinding();
  const revokeRiskApproval = useRevokeRiskApproval();
  const updateFindingStatus = useUpdateFindingStatus();
  const riskApprovalQuery = useRiskApprovalStatus(finding.id);

  const [reason, setReason] = useState<FindingCloseReason>("mitigated");
  const [justification, setJustification] = useState("");
  const [acceptedBy, setAcceptedBy] = useState("");
  const [expirationDate, setExpirationDate] = useState("");
  const [reactivateExpired, setReactivateExpired] = useState(true);
  const [isRiskModalOpen, setIsRiskModalOpen] = useState(false);
  // Local fallback: keeps the risk-accepted banner visible immediately after approval,
  // before the Dojo v2 finding refetch reflects risk_accepted=true.
  const [localRiskApproved, setLocalRiskApproved] = useState(false);

  // Once the fresh finding data confirms risk_accepted, drop the local flag.
  useEffect(() => {
    if (finding.riskAccepted) setLocalRiskApproved(false);
  }, [finding.riskAccepted]);

  const dialogRef = useRef<HTMLDivElement>(null);

  // Focus trap + Escape handler
  useEffect(() => {
    if (!isRiskModalOpen) return;
    const dialog = dialogRef.current;
    if (!dialog) return;

    const focusableSelector =
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
    const getFocusable = () => Array.from(dialog.querySelectorAll<HTMLElement>(focusableSelector));

    // Focus first element when modal opens
    const firstFocusable = getFocusable()[0];
    firstFocusable?.focus();

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setIsRiskModalOpen(false);
        return;
      }
      if (event.key === "Tab") {
        const focusable = getFocusable();
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [isRiskModalOpen]);

  const riskEnabled = riskApprovalQuery.data?.enabled ?? true;
  const currentApproval = riskApprovalQuery.data?.current ?? null;
  const showRiskBanner = !finding.active && (finding.riskAccepted || localRiskApproved);
  const isPendingAction =
    closeFinding.isPending || riskApproveFinding.isPending || revokeRiskApproval.isPending;
  const canSubmitRiskApproval =
    Boolean(justification.trim()) && !isPendingAction && !isLocked;

  const handleApply = () => {
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
    );
  };

  const handleConfirmRiskApproval = () => {
    if (!justification.trim()) return;
    riskApproveFinding.mutate(
      {
        id: finding.id,
        justification: justification.trim(),
        acceptedBy: acceptedBy.trim() || undefined,
        expirationDate: expirationDate || undefined,
        reactivateExpired,
      },
      {
        onSuccess: () => {
          setLocalRiskApproved(true);
          onApplied?.("risk_accepted");
          toast.push("Risk approval applied.", "success");
          setIsRiskModalOpen(false);
        },
        onError: (error) => {
          const message = error instanceof Error ? error.message : String(error);
          toast.push(`Risk approval failed: ${message}`, "error");
        },
      },
    );
  };

  const handleRevoke = () => {
    revokeRiskApproval.mutate(
      { id: finding.id },
      {
        onSuccess: () => {
          setLocalRiskApproved(false);
          onReopened?.();
          toast.push("Risk approval revoked. Finding is now active.", "success");
        },
        onError: (error) => {
          const message = error instanceof Error ? error.message : String(error);
          toast.push(`Revoke failed: ${message}`, "error");
        },
      },
    );
  };

  const openRiskModal = () => {
    setJustification("");
    setAcceptedBy("");
    setExpirationDate("");
    setReactivateExpired(true);
    setIsRiskModalOpen(true);
  };

  return (
    <div>
      <PermissionGate action="enable">
        <div className="flex flex-wrap items-end gap-2">
          {/* ── Active finding: Close + Risk Approval ── */}
          {finding.active ? (
            <div className="w-full max-w-3xl">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
                <div className="w-full sm:w-80">
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
                      <path d="m9 12 2 2 4-4" />
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
                            <div className="text-sm font-semibold text-slate-100">Workflow Guide</div>
                            <div className="mt-1 text-xs text-slate-400">Choose between final close status and risk approval, then apply the right action.</div>
                          </div>
                          <div className="mb-3 grid gap-2 sm:grid-cols-2">
                            <div className="rounded-xl border border-night-500 bg-night-800/70 p-3">
                              <div className="text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-400">Close Action</div>
                              <div className="mt-1 text-xs leading-5 text-slate-200">Use when triage is final for this record: fixed, false positive, out of scope, or duplicate.</div>
                            </div>
                            <div className="rounded-xl border border-night-500 bg-night-800/70 p-3">
                              <div className="text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-400">Risk Approval</div>
                              <div className="mt-1 text-xs leading-5 text-slate-200">Use when issue is valid, but business accepts the risk with documented justification and optional expiration.</div>
                            </div>
                          </div>
                          <div className="mb-2 flex-shrink-0 text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-400">Status Reference</div>
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
                <button
                  type="button"
                  className="inline-flex h-10 items-center gap-1.5 rounded-xl bg-brand-500 pl-3 pr-4 text-xs font-semibold text-night-900 disabled:opacity-50"
                  onClick={handleApply}
                  disabled={isPendingAction || isLocked}
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
                    <path d="m5 12 4 4L19 6" />
                  </svg>
                  Apply Close
                </button>
                <div className="hidden h-10 w-px bg-night-500 sm:block" aria-hidden="true" />
                {/* Risk Approval button — wrapped in span so title tooltip works even when disabled */}
                <span
                  title={!riskEnabled ? "Risk acceptance is not enabled for this product" : undefined}
                  className="inline-flex"
                >
                  <button
                    type="button"
                    className="inline-flex h-10 items-center gap-1.5 rounded-xl border border-brand-600/50 bg-transparent px-4 text-xs font-semibold text-brand-300 transition hover:border-brand-500/70 hover:text-brand-200 disabled:cursor-not-allowed disabled:opacity-40"
                    onClick={openRiskModal}
                    disabled={!riskEnabled || isPendingAction || isLocked}
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
                      <path d="M12 3 5 6v6c0 4.2 2.8 8.1 7 9 4.2-.9 7-4.8 7-9V6l-7-3Z" />
                      <path d="m9.5 12 1.7 1.7 3.3-3.4" />
                    </svg>
                    Risk Approval
                  </button>
                </span>
              </div>
            </div>
          ) : null}

          {/* ── Inactive + risk-accepted: compact inline row ── */}
          {showRiskBanner ? (
            <div className="flex flex-wrap items-center gap-2">
              <div className="inline-flex h-10 items-center gap-2 rounded-xl border border-amber-400/25 bg-amber-400/5 px-3">
                <svg
                  viewBox="0 0 24 24"
                  className="h-4 w-4 shrink-0 text-amber-400"
                  aria-hidden="true"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.9"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M12 3 5 6v6c0 4.2 2.8 8.1 7 9 4.2-.9 7-4.8 7-9V6l-7-3Z" />
                  <path d="m9.5 12 1.7 1.7 3.3-3.4" />
                </svg>
                <span className="text-xs font-semibold text-amber-300">Risk Accepted</span>
                {currentApproval?.acceptedBy ? (
                  <>
                    <span className="text-slate-600" aria-hidden="true">·</span>
                    <span className="text-xs text-slate-400">{currentApproval.acceptedBy}</span>
                  </>
                ) : null}
                {currentApproval?.expirationDate ? (
                  <>
                    <span className="text-slate-600" aria-hidden="true">·</span>
                    <span className="text-xs text-slate-500">
                      expires {formatDateForUI(currentApproval.expirationDate)}
                    </span>
                  </>
                ) : null}
              </div>
              <button
                type="button"
                className="inline-flex h-10 items-center gap-1.5 rounded-xl border border-night-500 px-3 text-xs font-semibold text-slate-400 transition hover:border-danger-500/40 hover:text-danger-300 disabled:opacity-50"
                onClick={handleRevoke}
                disabled={revokeRiskApproval.isPending || isLocked}
              >
                <svg
                  viewBox="0 0 24 24"
                  className="h-3.5 w-3.5"
                  aria-hidden="true"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M18 6 6 18M6 6l12 12" />
                </svg>
                Revoke
              </button>
            </div>
          ) : null}

          {/* ── Inactive + NOT risk-accepted: Reopen button ── */}
          {!finding.active && !showRiskBanner ? (
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
                <path d="M3 12a9 9 0 1 0 3-6.7" />
                <path d="M3 4v4h4" />
              </svg>
              Reopen
            </button>
          ) : null}

          {isLocked ? (
            <div className="text-xs text-amber-300">Locked by active bulk update.</div>
          ) : null}
        </div>

        {/* ── Risk Approval Modal — rendered in document.body via portal so fixed
             positioning is relative to the viewport, not a transformed ancestor ── */}
        {isRiskModalOpen ? createPortal(
          <div className="fixed inset-0 z-[1400] flex items-center justify-center p-4">
            <button
              type="button"
              className="absolute inset-0 bg-night-950/70 backdrop-blur-[1px]"
              aria-label="Close risk approval dialog"
              onClick={() => setIsRiskModalOpen(false)}
            />
            <div
              ref={dialogRef}
              role="dialog"
              aria-modal="true"
              aria-labelledby="risk-modal-title"
              className="relative z-10 flex w-full max-w-xl flex-col rounded-2xl border border-night-500 bg-night-900 shadow-panel"
              style={{ maxHeight: "min(calc(100dvh - 2rem), 700px)" }}
            >
              {/* Header */}
              <div className="shrink-0 rounded-t-2xl border-b border-night-500 bg-night-800/70 px-5 py-4">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <h3 id="risk-modal-title" className="text-sm font-semibold text-slate-100">Risk Approval</h3>
                    <p className="mt-1 text-xs leading-5 text-slate-400">
                      Use this when the team intentionally accepts the risk with a documented business reason.
                    </p>
                    {/* Finding context */}
                    <div className="mt-2 flex items-center gap-2">
                      <span className={`inline-flex rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
                        finding.severity === "Critical"
                          ? "border-danger-500/50 bg-danger-500/10 text-danger-500"
                          : finding.severity === "High"
                            ? "border-danger-500/30 bg-danger-500/10 text-danger-500/80"
                            : finding.severity === "Medium"
                              ? "border-amber-400/40 bg-amber-400/10 text-amber-400"
                              : "border-slate-500/40 bg-slate-500/10 text-slate-300"
                      }`}>
                        {finding.severity}
                      </span>
                      <span className="line-clamp-1 text-xs text-slate-300" title={finding.title}>
                        {finding.title}
                      </span>
                    </div>
                  </div>
                  <button
                    type="button"
                    className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-night-500 text-slate-300 hover:text-slate-100"
                    onClick={() => setIsRiskModalOpen(false)}
                    aria-label="Close dialog"
                  >
                    <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                      <path d="M18 6 6 18M6 6l12 12" />
                    </svg>
                  </button>
                </div>
              </div>

              {/* Body */}
              <div className="aist-scrollbar flex-1 space-y-4 overflow-y-auto px-5 py-4">
                <div>
                  <div className="mb-1 flex items-center gap-2">
                    <label htmlFor="risk-justification" className="text-xs text-slate-300">Risk Approval Reason</label>
                    <span className="rounded-full border border-danger-500/30 bg-danger-500/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.1em] text-danger-400">Required</span>
                  </div>
                  <p className="mb-2 text-xs text-slate-500">Briefly explain why this exception is acceptable and what compensating controls exist.</p>
                  <textarea
                    id="risk-justification"
                    className="mt-2 min-h-[100px] w-full resize-y rounded-xl border border-night-500 bg-night-600 px-3 py-2 text-sm text-white outline-none transition focus-visible:border-brand-600 focus-visible:ring-2 focus-visible:ring-brand-600/60"
                    value={justification}
                    onChange={(event) => setJustification(event.target.value)}
                    placeholder="Explain why this risk is accepted and what controls exist."
                    maxLength={JUSTIFICATION_MAX_LENGTH}
                  />
                  {/* Character counter */}
                  <div className={`mt-1 text-right text-[11px] ${
                    justification.length > JUSTIFICATION_MAX_LENGTH * 0.9
                      ? "text-amber-400"
                      : "text-slate-500"
                  }`}>
                    {justification.length}/{JUSTIFICATION_MAX_LENGTH}
                  </div>
                </div>
                <div className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <label className="text-xs text-slate-300">Accepted By (optional)</label>
                    <TextInput
                      className="mt-2"
                      value={acceptedBy}
                      onChange={(event) => setAcceptedBy(event.target.value)}
                      placeholder="Risk owner name or email"
                    />
                  </div>
                  <DateField
                    label="Expiration Date (optional)"
                    value={expirationDate}
                    onChange={setExpirationDate}
                    placeholder="No expiration"
                  />
                </div>
                <label className="inline-flex items-center gap-2 rounded-xl border border-night-500 bg-night-800/60 px-3 py-2 text-xs text-slate-300">
                  <input
                    type="checkbox"
                    className="h-4 w-4 rounded border-night-500 bg-night-600 accent-brand-500"
                    checked={reactivateExpired}
                    onChange={(event) => setReactivateExpired(event.target.checked)}
                  />
                  Reopen finding automatically when approval expires
                </label>
              </div>

              {/* Footer */}
              <div className="shrink-0 flex justify-end gap-2 rounded-b-2xl border-t border-night-500 bg-night-800/40 px-5 py-3">
                <button
                  type="button"
                  className="inline-flex h-10 items-center rounded-xl border border-night-500 px-4 text-xs font-semibold text-slate-300 hover:text-slate-100"
                  onClick={() => setIsRiskModalOpen(false)}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="inline-flex h-10 items-center rounded-xl bg-brand-500 px-4 text-xs font-semibold text-night-900 disabled:opacity-50"
                  onClick={handleConfirmRiskApproval}
                  disabled={!canSubmitRiskApproval}
                >
                  Confirm Risk Approval
                </button>
              </div>
            </div>
          </div>,
          document.body,
        ) : null}
      </PermissionGate>
    </div>
  );
}
