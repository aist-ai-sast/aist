import type { Severity } from "../types";

export const SEVERITY_ORDER: Severity[] = ["Critical", "High", "Medium", "Low", "Info"];

const SEVERITY_BADGE_CLASSES: Record<Severity, string> = {
  Critical: "border-danger-500/50 text-danger-500 bg-danger-500/10",
  High: "border-orange-400/45 text-orange-300 bg-orange-500/10",
  Medium: "border-amber-400/40 text-amber-400 bg-amber-400/10",
  Low: "border-slate-500/40 text-slate-300 bg-slate-500/10",
  Info: "border-slate-500/40 text-slate-300 bg-slate-500/10",
};

const SEVERITY_BAR_CLASSES: Record<Severity, string> = {
  Critical: "bg-danger-500",
  High: "bg-orange-400",
  Medium: "bg-amber-400",
  Low: "bg-slate-400",
  Info: "bg-slate-500",
};

export function severityBadgeClass(severity: Severity) {
  return SEVERITY_BADGE_CLASSES[severity];
}

export function severityBarClass(severity: Severity) {
  return SEVERITY_BAR_CLASSES[severity];
}

const FINDING_STATUS_BADGE_CLASSES = {
  Mitigated: "border-emerald-400/40 bg-emerald-400/10 text-emerald-300",
  "Risk Accepted": "border-amber-400/40 bg-amber-400/10 text-amber-300",
  "False Positive": "border-purple-400/40 bg-purple-400/10 text-purple-300",
  "Out of Scope": "border-slate-400/40 bg-slate-400/10 text-slate-300",
  Duplicate: "border-slate-400/40 bg-slate-400/10 text-slate-300",
  Active: "border-night-500 bg-night-900 text-slate-200",
  Inactive: "border-night-500 bg-night-900 text-slate-200",
  "Non-Active": "border-night-500 bg-night-900 text-slate-200",
  "Under Review": "border-sky-400/40 bg-sky-400/10 text-sky-300",
  Verified: "border-brand-500/40 bg-brand-500/10 text-brand-300",
} as const;

export type FindingStatusBadge = keyof typeof FINDING_STATUS_BADGE_CLASSES;

export function findingStatusBadgeClass(status: FindingStatusBadge) {
  return FINDING_STATUS_BADGE_CLASSES[status];
}

const PIPELINE_STATUS_LABELS: Record<string, string> = {
  ADMITTED: "Admitted",
  EXECUTING: "Executing",
  UPLOADING_RESULTS: "Uploading results",
  FINDING_POSTPROCESSING: "Finding post-processing",
  WAITING_DEDUPLICATION_TO_FINISH: "Waiting for deduplication",
  WAITING_CONFIRMATION_TO_PUSH_TO_AI: "Waiting for AI confirmation",
  PUSH_TO_AI: "Sending to AI",
  WAITING_RESULT_FROM_AI: "Waiting for AI result",
  FINISHED: "Finished",
  FINISHED_WITH_WARNINGS: "Finished with warnings",
};

export function pipelineStatusLabel(status: string) {
  const upper = status.toUpperCase();
  return PIPELINE_STATUS_LABELS[upper] ?? status;
}

export function pipelineStatusBadgeClass(status: string) {
  const upper = status.toUpperCase();
  if (upper === "ADMITTED") return "border-sky-400/50 text-sky-300 bg-sky-400/10";
  if (upper === "EXECUTING") return "border-brand-500/50 text-brand-300 bg-brand-500/10";
  if (upper.includes("FAIL")) return "border-danger-500/50 text-danger-500 bg-danger-500/10";
  if (upper.includes("WARNING")) return "border-amber-400/50 text-amber-300 bg-amber-400/10";
  if (upper.includes("FINISH")) return "border-brand-600/50 text-brand-500 bg-brand-600/10";
  if (upper.includes("START")) return "border-brand-600/50 text-brand-500 bg-brand-600/10";
  return "border-slate-500/40 text-slate-300 bg-night-700";
}
