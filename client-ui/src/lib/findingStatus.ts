import type { Finding } from "../types";
import type { FindingStatusBadge } from "./badgeStyles";

type FindingStatusRule = {
  key: FindingStatusBadge;
  enabled: (finding: Finding) => boolean;
};

const FINDING_STATUS_RULES: FindingStatusRule[] = [
  { key: "Mitigated", enabled: (finding) => Boolean(finding.isMitigated) },
  { key: "Risk Accepted", enabled: (finding) => Boolean(finding.riskAccepted) },
  { key: "False Positive", enabled: (finding) => Boolean(finding.falsePositive) },
  { key: "Out of Scope", enabled: (finding) => Boolean(finding.outOfScope) },
  { key: "Duplicate", enabled: (finding) => Boolean(finding.duplicate) },
];

/** Selects the endpoint and reproduction-evidence layout used for DAST findings. */
export function isDastFinding(finding: Finding): boolean {
  return Boolean(finding.dynamicFinding) || Boolean(finding.tags?.includes("dast"));
}

export function getFindingStatusBadges(finding: Finding): FindingStatusBadge[] {
  const statuses = FINDING_STATUS_RULES
    .filter((rule) => rule.enabled(finding))
    .map((rule) => rule.key);
  if (statuses.length > 0) {
    return statuses;
  }
  return [finding.active ? "Active" : "Non-Active"];
}
