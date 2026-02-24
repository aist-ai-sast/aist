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

export function pipelineStatusBadgeClass(status: string) {
  const upper = status.toUpperCase();
  if (upper.includes("FAIL")) return "border-danger-500/50 text-danger-500 bg-danger-500/10";
  if (upper.includes("WARNING")) return "border-amber-400/50 text-amber-300 bg-amber-400/10";
  if (upper.includes("FINISH")) return "border-brand-600/50 text-brand-500 bg-brand-600/10";
  if (upper.includes("START")) return "border-brand-600/50 text-brand-500 bg-brand-600/10";
  return "border-slate-500/40 text-slate-300 bg-night-700";
}
