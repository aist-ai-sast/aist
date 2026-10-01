import type { FindingsFilterUrlState } from "./findingsFilterUrl";
import { AI_STATUS_OPTIONS, RISK_STATE_LABELS, WORK_ITEM_STATUS_OPTIONS, optionLabel } from "./findingsFilterOptions";
import { setTagState } from "./tagFilter";

export type ActiveFilterTone = "neutral" | "include" | "exclude";

/**
 * One chip in the active-filters bar. ``patch`` is the state change that removes
 * exactly this chip, so the page applies it without knowing what the chip meant.
 */
export type ActiveFilterChip = {
  id: string;
  label: string;
  value: string;
  tone: ActiveFilterTone;
  patch: Partial<FindingsFilterUrlState>;
};

type DescribeOptions = {
  projectName?: (projectId: number) => string | undefined;
};

function rangeValue(from: string, to: string): string {
  if (from && to) return `${from} – ${to}`;
  if (from) return `from ${from}`;
  return `until ${to}`;
}

export function describeActiveFilters(
  state: FindingsFilterUrlState,
  options: DescribeOptions = {},
): ActiveFilterChip[] {
  const chips: ActiveFilterChip[] = [];
  const neutral = (id: string, label: string, value: string, patch: Partial<FindingsFilterUrlState>) =>
    chips.push({ id, label, value, tone: "neutral", patch });

  if (state.projectId) {
    neutral("project", "Project", options.projectName?.(state.projectId) ?? `#${state.projectId}`, {
      projectId: undefined,
      projectVersion: "",
    });
  }
  if (state.pipelineId) neutral("pipeline", "Pipeline", state.pipelineId, { pipelineId: undefined });
  if (state.projectVersion) neutral("version", "Version", state.projectVersion, { projectVersion: "" });
  if (state.title) neutral("title", "Title", state.title, { title: "" });
  if (state.file) neutral("file", "File", state.file, { file: "" });
  if (state.cwe) neutral("cwe", "CWE", state.cwe, { cwe: "" });
  if (state.status !== "All") neutral("status", "Status", state.status, { status: "All" });

  state.severities.forEach((severity) => {
    neutral(`severity:${severity}`, "Severity", severity, {
      severities: state.severities.filter((item) => item !== severity),
    });
  });
  state.risk.forEach((risk) => {
    neutral(`risk:${risk}`, "Risk", RISK_STATE_LABELS[risk], {
      risk: state.risk.filter((item) => item !== risk),
    });
  });

  if (state.createdFrom || state.createdTo) {
    neutral("created", "Created", rangeValue(state.createdFrom, state.createdTo), { createdFrom: "", createdTo: "" });
  }
  if (state.statusUpdatedFrom || state.statusUpdatedTo) {
    neutral("status_updated", "Status updated", rangeValue(state.statusUpdatedFrom, state.statusUpdatedTo), {
      statusUpdatedFrom: "",
      statusUpdatedTo: "",
    });
  }
  if (state.mitigatedFrom || state.mitigatedTo) {
    neutral("mitigated", "Mitigated", rangeValue(state.mitigatedFrom, state.mitigatedTo), {
      mitigatedFrom: "",
      mitigatedTo: "",
    });
  }

  if (state.workItemLinkedFrom || state.workItemLinkedTo) {
    neutral("work_item_linked", "Ticketed", rangeValue(state.workItemLinkedFrom, state.workItemLinkedTo), {
      workItemLinkedFrom: "",
      workItemLinkedTo: "",
    });
  }

  const tagLabel = state.tags.include.length > 1 && state.tags.matchMode === "all" ? "All tags" : "Tag";
  state.tags.include.forEach((tag) => {
    chips.push({ id: `tag:include:${tag}`, label: tagLabel, value: tag, tone: "include", patch: { tags: setTagState(state.tags, tag, "none") } });
  });
  state.tags.exclude.forEach((tag) => {
    chips.push({ id: `tag:exclude:${tag}`, label: "Not tag", value: tag, tone: "exclude", patch: { tags: setTagState(state.tags, tag, "none") } });
  });

  if (state.aiStatus && state.aiStatus !== "All") {
    neutral("ai_status", "AI", optionLabel(AI_STATUS_OPTIONS, state.aiStatus), { aiStatus: "All" });
  }
  if (state.workItemStatus !== "all") {
    neutral("work_item", "Work item", optionLabel(WORK_ITEM_STATUS_OPTIONS, state.workItemStatus), { workItemStatus: "all" });
  }
  return chips;
}
