import { buildFindingsFilterSearch, type FindingsFilterUrlState } from "./findingsFilterUrl";

export type DrilldownPatch = Partial<FindingsFilterUrlState>;

/**
 * Findings-page query for a dashboard click: the dashboard's whole filter with
 * the clicked slice on top. A slice replaces the field it names; every other
 * filter is kept, so the list shows exactly the findings behind the segment.
 */
export function drilldownSearch(state: FindingsFilterUrlState, patch: DrilldownPatch = {}): string {
  return buildFindingsFilterSearch({ ...state, ...patch }).toString();
}

function toDateParam(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function addDays(date: Date, days: number): Date {
  const next = new Date(date);
  next.setUTCDate(next.getUTCDate() + days);
  return next;
}

/** Age bucket of the aging heatmap as a created-date range (both bounds inclusive). */
export function agingBucketRange(bucket: string, now: Date): Pick<FindingsFilterUrlState, "createdFrom" | "createdTo"> {
  switch (bucket) {
    case "0_7":
      return { createdFrom: toDateParam(addDays(now, -7)), createdTo: toDateParam(now) };
    case "8_30":
      return { createdFrom: toDateParam(addDays(now, -30)), createdTo: toDateParam(addDays(now, -8)) };
    case "31_90":
      return { createdFrom: toDateParam(addDays(now, -90)), createdTo: toDateParam(addDays(now, -31)) };
    default:
      return { createdFrom: "", createdTo: toDateParam(addDays(now, -91)) };
  }
}

/** ISO week start ("2026-09-21") as a seven-day created-date range. */
export function weekRange(weekStart: string): Pick<FindingsFilterUrlState, "createdFrom" | "createdTo"> {
  return {
    createdFrom: weekStart,
    createdTo: toDateParam(addDays(new Date(`${weekStart}T00:00:00Z`), 6)),
  };
}
