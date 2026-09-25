import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import type { FilterPanelProps } from "../components/FilterPanel";
import type { Project, RiskState, Severity } from "../types";
import { type ActiveFilterChip, describeActiveFilters } from "./findingsActiveFilters";
import {
  buildFindingsFilterSearch,
  DEFAULT_FINDINGS_FILTERS,
  type FindingsFilterUrlState,
  parseFindingsFiltersFromSearch,
  toFindingStatusFilter,
} from "./findingsFilterUrl";
import { useFindingTagsByProject } from "./queries";
import { isSameTagFilter, pruneTagFilter } from "./tagFilter";

export const FILTER_TEXT_DEBOUNCE_MS = 300;

/** Free-text fields (and the tag picker) settle before they reach the URL and the API. */
type DebouncedFields = Pick<FindingsFilterUrlState, "file" | "title" | "cwe" | "projectVersion" | "tags">;

function pickDebouncedFields(state: FindingsFilterUrlState): DebouncedFields {
  return {
    file: state.file,
    title: state.title,
    cwe: state.cwe,
    projectVersion: state.projectVersion,
    tags: state.tags,
  };
}

function filterKey(state: FindingsFilterUrlState): string {
  return buildFindingsFilterSearch(state).toString();
}

type UseFindingsFilterStateOptions = {
  /** Projects the user can see: the panel's project options and the chips' project names. */
  projects: Project[];
  /** Page-owned query parameters written next to the filter (the Findings page's ``page``). */
  extraParams?: Record<string, string>;
};

/**
 * The Findings filter as page state, kept in the URL.
 *
 * ``state`` drives the inputs; ``debounced`` is what the URL and the API see.
 * A URL change from outside (back/forward, a link) re-hydrates both at once, so
 * a lagging debounce can never write the previous text back.
 */
export function useFindingsFilterState({ projects, extraParams }: UseFindingsFilterStateOptions) {
  const location = useLocation();
  const navigate = useNavigate();
  const initialRef = useRef<FindingsFilterUrlState | null>(null);
  if (!initialRef.current) {
    initialRef.current = parseFindingsFiltersFromSearch(new URLSearchParams(location.search));
  }
  const [state, setState] = useState<FindingsFilterUrlState>(initialRef.current);
  const [debouncedFields, setDebouncedFields] = useState<DebouncedFields>(() =>
    pickDebouncedFields(initialRef.current ?? DEFAULT_FINDINGS_FILTERS),
  );
  const lastWrittenSearch = useRef(location.search);

  const pendingFieldsKey = JSON.stringify(pickDebouncedFields(state));
  useEffect(() => {
    const timeout = window.setTimeout(() => {
      setDebouncedFields((current) =>
        JSON.stringify(current) === pendingFieldsKey ? current : (JSON.parse(pendingFieldsKey) as DebouncedFields),
      );
    }, FILTER_TEXT_DEBOUNCE_MS);
    return () => window.clearTimeout(timeout);
  }, [pendingFieldsKey]);

  const debounced = useMemo<FindingsFilterUrlState>(
    () => ({ ...state, ...debouncedFields }),
    [state, debouncedFields],
  );

  // Hydrate from a URL this hook did not write itself.
  useEffect(() => {
    if (location.search === lastWrittenSearch.current) return;
    lastWrittenSearch.current = location.search;
    const parsed = parseFindingsFiltersFromSearch(new URLSearchParams(location.search));
    setState((current) => (filterKey(current) === filterKey(parsed) ? current : parsed));
    setDebouncedFields(pickDebouncedFields(parsed));
  }, [location.search]);

  const extraKey = new URLSearchParams(extraParams).toString();
  useEffect(() => {
    const params = buildFindingsFilterSearch(debounced);
    new URLSearchParams(extraKey).forEach((value, key) => params.set(key, value));
    const next = params.toString();
    const nextSearch = next ? `?${next}` : "";
    if (nextSearch === lastWrittenSearch.current) return;
    lastWrittenSearch.current = nextSearch;
    navigate({ pathname: location.pathname, search: nextSearch }, { replace: true });
  }, [debounced, extraKey, location.pathname, navigate]);

  const apply = useCallback((patch: Partial<FindingsFilterUrlState>) => {
    setState((current) => ({ ...current, ...patch }));
  }, []);

  const clearAll = useCallback(() => {
    setState(DEFAULT_FINDINGS_FILTERS);
    setDebouncedFields(pickDebouncedFields(DEFAULT_FINDINGS_FILTERS));
  }, []);

  // A tag that no longer exists in the selected project's scope is dropped from
  // the selection. A failing tag query only leaves the options empty.
  const tagsQuery = useFindingTagsByProject(state.projectId);
  const availableTags = useMemo(() => tagsQuery.data?.names ?? [], [tagsQuery.data]);
  useEffect(() => {
    if (!tagsQuery.isSuccess) return;
    setState((current) => {
      const tags = pruneTagFilter(current.tags, availableTags);
      return isSameTagFilter(tags, current.tags) ? current : { ...current, tags };
    });
  }, [availableTags, tagsQuery.isSuccess]);

  const projectsById = useMemo(() => new Map(projects.map((project) => [project.id, project])), [projects]);
  const chips = useMemo<ActiveFilterChip[]>(
    () => describeActiveFilters(state, { projectName: (projectId) => projectsById.get(projectId)?.name }),
    [state, projectsById],
  );
  const removeChip = useCallback((chip: ActiveFilterChip) => apply(chip.patch), [apply]);

  const panelProps = useMemo<FilterPanelProps>(
    () => ({
      products: projects,
      selectedProjectId: state.projectId,
      onProjectChange: (projectId) => apply({ projectId, projectVersion: "" }),
      selectedSeverities: state.severities,
      onSeveritiesChange: (severities) => apply({ severities: severities as Severity[] }),
      selectedFile: state.file,
      onFileChange: (file) => apply({ file }),
      createdFrom: state.createdFrom,
      onCreatedFromChange: (createdFrom) => apply({ createdFrom }),
      createdTo: state.createdTo,
      onCreatedToChange: (createdTo) => apply({ createdTo }),
      selectedProjectVersion: state.projectVersion,
      onProjectVersionChange: (projectVersion) => apply({ projectVersion }),
      selectedTitle: state.title,
      onTitleChange: (title) => apply({ title }),
      selectedStatus: state.status,
      onStatusChange: (status) => apply({ status: toFindingStatusFilter(status) }),
      selectedRisk: state.risk,
      onRiskChange: (risk) => apply({ risk: risk as RiskState[] }),
      selectedCwe: state.cwe,
      onCweChange: (cwe) => apply({ cwe }),
      availableTags,
      tagCounts: tagsQuery.data?.counts,
      tagFilter: state.tags,
      onTagFilterChange: (tags) => apply({ tags }),
      selectedAiResponse: state.aiStatus,
      onAiResponseChange: (aiStatus) => apply({ aiStatus }),
      selectedWorkItemStatus: state.workItemStatus,
      onWorkItemStatusChange: (workItemStatus) =>
        apply({ workItemStatus: workItemStatus as FindingsFilterUrlState["workItemStatus"] }),
      onClearAll: clearAll,
    }),
    [apply, availableTags, clearAll, projects, state, tagsQuery.data],
  );

  return { state, debounced, apply, clearAll, chips, removeChip, panelProps };
}
