import type { FindingFilters, RiskState, Severity } from "../types";

const SEVERITY_ORDER: Severity[] = ["Critical", "High", "Medium", "Low", "Info"];
const RISK_ORDER: RiskState[] = ["risk_accepted", "under_review", "mitigated"];

export type FindingsFilterUrlState = {
  projectId?: number;
  pipelineId?: string;
  title: string;
  createdFrom: string;
  createdTo: string;
  statusUpdatedFrom: string;
  statusUpdatedTo: string;
  mitigatedFrom: string;
  mitigatedTo: string;
  projectVersion: string;
  file: string;
  cwe: string;
  severities: Severity[];
  tags: string[];
  status: "All" | "Active" | "Non-Active";
  risk: RiskState[];
  aiStatus: string;
  hasWorkItem: "all" | "yes" | "no";
};

export type FindingStatusFilter = FindingsFilterUrlState["status"];

export const DEFAULT_FINDINGS_FILTERS: FindingsFilterUrlState = {
  projectId: undefined,
  pipelineId: undefined,
  title: "",
  createdFrom: "",
  createdTo: "",
  statusUpdatedFrom: "",
  statusUpdatedTo: "",
  mitigatedFrom: "",
  mitigatedTo: "",
  projectVersion: "",
  file: "",
  cwe: "",
  severities: [],
  tags: [],
  status: "All",
  risk: [],
  aiStatus: "All",
  hasWorkItem: "all",
};

const FINDING_STATUS_VALUES = new Set<FindingStatusFilter>(["All", "Active", "Non-Active"]);

export function toFindingStatusFilter(value: string): FindingStatusFilter {
  if (FINDING_STATUS_VALUES.has(value as FindingStatusFilter)) {
    return value as FindingStatusFilter;
  }
  return "All";
}

function parseCsv(raw: string | null): string[] {
  return (raw ?? "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function orderByPriority<T extends string>(items: T[], order: readonly T[]): T[] {
  const rank = new Map(order.map((item, index) => [item, index]));
  return [...new Set(items)].sort((left, right) => {
    const leftRank = rank.get(left) ?? Number.MAX_SAFE_INTEGER;
    const rightRank = rank.get(right) ?? Number.MAX_SAFE_INTEGER;
    if (leftRank !== rightRank) return leftRank - rightRank;
    return left.localeCompare(right);
  });
}

export function parseFindingsFiltersFromSearch(params: URLSearchParams): FindingsFilterUrlState {
  const projectRaw = params.get("project") ?? params.get("project_id");
  const projectParsed = projectRaw ? Number(projectRaw) : Number.NaN;
  const projectId = Number.isFinite(projectParsed) ? projectParsed : undefined;

  const active = (params.get("active") ?? "").toLowerCase();
  let status: FindingsFilterUrlState["status"] = "All";
  if (active === "true" || active === "1") {
    status = "Active";
  } else if (active === "false" || active === "0") {
    status = "Non-Active";
  } else {
    const fallbackStatus = (params.get("status") ?? "").toLowerCase();
    if (fallbackStatus === "active" || fallbackStatus === "enabled") {
      status = "Active";
    } else if (fallbackStatus === "non-active" || fallbackStatus === "disabled") {
      status = "Non-Active";
    }
  }

  const riskRaw: RiskState[] = [];
  const riskAccepted = (params.get("risk_accepted") ?? "").toLowerCase();
  const underReview = (params.get("under_review") ?? "").toLowerCase();
  const isMitigated = (params.get("is_mitigated") ?? "").toLowerCase();
  if (riskAccepted === "true" || riskAccepted === "1") riskRaw.push("risk_accepted");
  if (underReview === "true" || underReview === "1") riskRaw.push("under_review");
  if (isMitigated === "true" || isMitigated === "1") riskRaw.push("mitigated");

  return {
    projectId,
    pipelineId: params.get("pipeline") ?? params.get("pipeline_id") ?? undefined,
    title: params.get("title") ?? "",
    createdFrom: params.get("created_from") ?? params.get("created_gte") ?? "",
    createdTo: params.get("created_to") ?? params.get("created_lte") ?? "",
    statusUpdatedFrom:
      params.get("processed_from")
      ?? params.get("processed_gte")
      ?? params.get("status_updated_from")
      ?? params.get("status_updated_gte")
      ?? "",
    statusUpdatedTo:
      params.get("processed_to")
      ?? params.get("processed_lte")
      ?? params.get("status_updated_to")
      ?? params.get("status_updated_lte")
      ?? "",
    mitigatedFrom: params.get("mitigated_from") ?? params.get("mitigated_gte") ?? "",
    mitigatedTo: params.get("mitigated_to") ?? params.get("mitigated_lte") ?? "",
    projectVersion: params.get("project_version") ?? "",
    file: params.get("file") ?? "",
    cwe: params.get("cwe") ?? "",
    severities: orderByPriority(parseCsv(params.get("severity")) as Severity[], SEVERITY_ORDER),
    tags: [...new Set(parseCsv(params.get("tags")))].sort((left, right) => left.localeCompare(right)),
    status,
    risk: orderByPriority(riskRaw, RISK_ORDER),
    aiStatus: params.get("ai_status") || "All",
    hasWorkItem: (params.get("has_work_item") === "yes" || params.get("has_work_item") === "true"
      ? "yes"
      : params.get("has_work_item") === "no" || params.get("has_work_item") === "false"
        ? "no"
        : "all") as FindingsFilterUrlState["hasWorkItem"],
  };
}

export function buildFindingsFilterSearch(state: FindingsFilterUrlState): URLSearchParams {
  const params = new URLSearchParams();
  if (state.projectId) params.set("project_id", String(state.projectId));
  if (state.pipelineId) params.set("pipeline_id", state.pipelineId);
  if (state.title) params.set("title", state.title);
  if (state.createdFrom) params.set("created_gte", state.createdFrom);
  if (state.createdTo) params.set("created_lte", state.createdTo);
  if (state.statusUpdatedFrom) params.set("processed_gte", state.statusUpdatedFrom);
  if (state.statusUpdatedTo) params.set("processed_lte", state.statusUpdatedTo);
  if (state.mitigatedFrom) params.set("mitigated_gte", state.mitigatedFrom);
  if (state.mitigatedTo) params.set("mitigated_lte", state.mitigatedTo);
  if (state.projectVersion) params.set("project_version", state.projectVersion);
  if (state.file) params.set("file", state.file);
  if (state.cwe) params.set("cwe", state.cwe);
  if (state.severities.length > 0) params.set("severity", orderByPriority(state.severities, SEVERITY_ORDER).join(","));
  if (state.tags.length > 0) {
    const normalizedTags = [...new Set(state.tags.map((tag) => tag.trim()).filter(Boolean))]
      .sort((left, right) => left.localeCompare(right));
    if (normalizedTags.length > 0) params.set("tags", normalizedTags.join(","));
  }
  if (state.status === "Active") params.set("active", "true");
  if (state.status === "Non-Active") params.set("active", "false");
  if (state.risk.includes("risk_accepted")) params.set("risk_accepted", "true");
  if (state.risk.includes("under_review")) params.set("under_review", "true");
  if (state.risk.includes("mitigated")) params.set("is_mitigated", "true");
  if (state.aiStatus && state.aiStatus !== "All") params.set("ai_status", state.aiStatus);
  if (state.hasWorkItem && state.hasWorkItem !== "all") params.set("has_work_item", state.hasWorkItem);
  return params;
}

export function toFindingsApiFilters(
  state: FindingsFilterUrlState,
  options?: { limit?: number; offset?: number; ordering?: string },
): FindingFilters {
  return {
    projectId: state.projectId,
    pipelineId: state.pipelineId,
    title: state.title || undefined,
    createdGte: state.createdFrom || undefined,
    createdLte: state.createdTo || undefined,
    statusUpdatedGte: state.statusUpdatedFrom || undefined,
    statusUpdatedLte: state.statusUpdatedTo || undefined,
    processedGte: state.statusUpdatedFrom || undefined,
    processedLte: state.statusUpdatedTo || undefined,
    mitigatedGte: state.mitigatedFrom || undefined,
    mitigatedLte: state.mitigatedTo || undefined,
    projectVersion: state.projectVersion || undefined,
    file: state.file || undefined,
    aiStatus:
      state.aiStatus === "All"
        ? undefined
        : (state.aiStatus as "has_ai" | "no_ai" | "ai_tp" | "ai_fp" | "ai_u"),
    hasWorkItem: state.hasWorkItem !== "all" ? state.hasWorkItem : undefined,
    severities: state.severities.length ? state.severities : undefined,
    status: state.status === "Active" ? "enabled" : state.status === "Non-Active" ? "disabled" : undefined,
    riskStates: state.risk.length ? state.risk : undefined,
    cwe: state.cwe || undefined,
    tags: state.tags.length ? state.tags : undefined,
    limit: options?.limit,
    offset: options?.offset,
    ordering: options?.ordering,
  };
}
