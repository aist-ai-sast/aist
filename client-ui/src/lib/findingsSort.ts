export type FindingsSortKey = "severity" | "date" | "title";
export type SortDirection = "asc" | "desc";

export const FINDINGS_SORT_OPTIONS: Array<{ value: FindingsSortKey; label: string }> = [
  { value: "severity", label: "Severity" },
  { value: "date", label: "Date" },
  { value: "title", label: "Title" },
];

export function buildFindingsOrdering(sort: FindingsSortKey, direction: SortDirection): string {
  if (sort === "severity") {
    // DefectDojo numerical_severity has reversed semantics for +/-.
    return direction === "desc" ? "numerical_severity" : "-numerical_severity";
  }
  const field = sort === "date" ? "date" : "title";
  return `${direction === "desc" ? "-" : ""}${field}`;
}
