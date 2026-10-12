import type { ActiveFilterTone } from "./findingsActiveFilters";

export type PipelinesFilterState = {
  projectId?: number;
  status: string;
  search: string;
  createdFrom: string;
  createdTo: string;
};

export const EMPTY_PIPELINES_FILTERS: PipelinesFilterState = {
  projectId: undefined,
  status: "all",
  search: "",
  createdFrom: "",
  createdTo: "",
};

/**
 * One chip in the pipelines active-filter bar. `patch` is the state change that removes
 * exactly this chip, so the page applies it without having to know what the chip meant —
 * the same contract the findings chips use.
 */
export type PipelinesFilterChip = {
  id: string;
  label: string;
  value: string;
  tone: ActiveFilterTone;
  patch: Partial<PipelinesFilterState>;
};

type DescribeOptions = {
  projectName?: (projectId: number) => string | undefined;
  statusLabel?: (status: string) => string | undefined;
};

function rangeValue(from: string, to: string): string {
  if (from && to) return `${from} – ${to}`;
  if (from) return `from ${from}`;
  return `until ${to}`;
}

export function describePipelinesFilters(
  state: PipelinesFilterState,
  options: DescribeOptions = {},
): PipelinesFilterChip[] {
  const chips: PipelinesFilterChip[] = [];
  const add = (id: string, label: string, value: string, patch: Partial<PipelinesFilterState>) =>
    chips.push({ id, label, value, tone: "neutral", patch });

  if (state.projectId) {
    add("project", "Project", options.projectName?.(state.projectId) ?? `#${state.projectId}`, {
      projectId: undefined,
    });
  }
  if (state.status && state.status !== "all") {
    add("status", "Status", options.statusLabel?.(state.status) ?? state.status, { status: "all" });
  }
  if (state.search) {
    add("search", "Branch / Commit", state.search, { search: "" });
  }
  if (state.createdFrom || state.createdTo) {
    add("created", "Created", rangeValue(state.createdFrom, state.createdTo), {
      createdFrom: "",
      createdTo: "",
    });
  }
  return chips;
}
