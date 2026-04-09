import { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import FilterPanel from "../components/FilterPanel";
import FindingCard from "../components/FindingCard";
import SegmentedSortControl from "../components/SegmentedSortControl";
import type { Finding, RiskState, Severity } from "../types";
import {
  useAiFindingResponses,
  useFindingTagsByProject,
  useFindingsPage,
  useProjects,
  useRiskApprovalStatus,
} from "../lib/queries";
import { useToast } from "../components/ToastProvider";
import PaginationBar from "../components/PaginationBar";
import { buildFindingsOrdering, FINDINGS_SORT_OPTIONS, type FindingsSortKey } from "../lib/findingsSort";
import PageErrorState from "../components/PageErrorState";
import SelectField from "../components/SelectField";
import SkeletonBlock from "../components/SkeletonBlock";
import TextInput from "../components/TextInput";
import { ApiError, toUserMessage } from "../lib/api";
import { type FindingCloseReason, useBulkFindingStatus } from "../lib/mutations";
import {
  buildFindingsFilterSearch,
  DEFAULT_FINDINGS_FILTERS,
  type FindingStatusFilter,
  parseFindingsFiltersFromSearch,
  toFindingStatusFilter,
  toFindingsApiFilters,
} from "../lib/findingsFilterUrl";
import { useDebouncedValue } from "../lib/useDebouncedValue";
import PermissionGate from "../components/PermissionGate";

const DetailPanel = lazy(() => import("../components/DetailPanel"));

const BULK_ACTION_OPTIONS: { value: string; label: string }[] = [
  { value: "close", label: "Close Findings" },
  { value: "reopen", label: "Reopen Findings" },
  { value: "risk_accept", label: "Risk Accept Findings" },
];

const BULK_CLOSE_REASON_OPTIONS: { value: string; label: string }[] = [
  { value: "mitigated", label: "Mitigated" },
  { value: "false_positive", label: "False Positive" },
  { value: "out_of_scope", label: "Out of Scope" },
  { value: "duplicate", label: "Duplicate" },
];

export default function FindingsPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const initialUrlFiltersRef = useRef<ReturnType<typeof parseFindingsFiltersFromSearch> | null>(null);
  if (!initialUrlFiltersRef.current) {
    initialUrlFiltersRef.current = parseFindingsFiltersFromSearch(new URLSearchParams(location.search));
  }
  const initialUrlFilters = initialUrlFiltersRef.current;
  const [selectedProjectId, setSelectedProjectId] = useState<number | undefined>(initialUrlFilters.projectId);
  const [selectedSeverities, setSelectedSeverities] = useState<Severity[]>(initialUrlFilters.severities);
  const [selectedStatus, setSelectedStatus] = useState<FindingStatusFilter>(initialUrlFilters.status);
  const [selectedRisk, setSelectedRisk] = useState<RiskState[]>(initialUrlFilters.risk);
  const [selectedAiResponse, setSelectedAiResponse] = useState<string>(initialUrlFilters.aiStatus);
  const [selectedSort, setSelectedSort] = useState<FindingsSortKey>("severity");
  const [selectedSortDirection, setSelectedSortDirection] = useState<"asc" | "desc">("desc");
  const [selectedCwe, setSelectedCwe] = useState<string>(initialUrlFilters.cwe);
  const [selectedFile, setSelectedFile] = useState<string>(initialUrlFilters.file);
  const [selectedTitle, setSelectedTitle] = useState<string>(initialUrlFilters.title);
  const [selectedProjectVersion, setSelectedProjectVersion] = useState<string>(initialUrlFilters.projectVersion);
  const [selectedTags, setSelectedTags] = useState<string[]>(initialUrlFilters.tags);
  const [selectedPipelineId, setSelectedPipelineId] = useState<string | undefined>(initialUrlFilters.pipelineId);
  const [selectedWorkItemStatus, setSelectedWorkItemStatus] = useState(initialUrlFilters.workItemStatus);
  const [createdFrom, setCreatedFrom] = useState<string>(initialUrlFilters.createdFrom);
  const [createdTo, setCreatedTo] = useState<string>(initialUrlFilters.createdTo);
  const [statusUpdatedFrom, setStatusUpdatedFrom] = useState<string>(initialUrlFilters.statusUpdatedFrom);
  const [statusUpdatedTo, setStatusUpdatedTo] = useState<string>(initialUrlFilters.statusUpdatedTo);
  const [mitigatedFrom, setMitigatedFrom] = useState<string>(initialUrlFilters.mitigatedFrom);
  const [mitigatedTo, setMitigatedTo] = useState<string>(initialUrlFilters.mitigatedTo);
  const [findingOverrides, setFindingOverrides] = useState<Record<number, Partial<Finding>>>({});
  const [selectedFindingIds, setSelectedFindingIds] = useState<number[]>([]);
  const [bulkEditMode, setBulkEditMode] = useState<boolean>(false);
  const [bulkAction, setBulkAction] = useState<"close" | "reopen" | "risk_accept">("close");
  const [bulkCloseReason, setBulkCloseReason] = useState<FindingCloseReason>("mitigated");
  const [bulkReasonNote, setBulkReasonNote] = useState<string>("");
  const [bulkLockedFindingIds, setBulkLockedFindingIds] = useState<number[]>([]);
  const hasHydratedStatusFromUrl = useRef(false);
  const lastWrittenSearch = useRef(location.search);
  const toast = useToast();
  const searchParams = useMemo(
    () => new URLSearchParams(location.search),
    [location.search],
  );
  const [pageSize, setPageSize] = useState<number>(50);
  const [pageIndex, setPageIndex] = useState<number>(() => {
    const raw = new URLSearchParams(location.search).get("page");
    const parsed = raw ? Number(raw) - 1 : 0;
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
  });
  const debouncedFile = useDebouncedValue(selectedFile, 300);
  const debouncedTitle = useDebouncedValue(selectedTitle, 300);
  const debouncedCwe = useDebouncedValue(selectedCwe, 300);
  const debouncedProjectVersion = useDebouncedValue(selectedProjectVersion, 300);
  const debouncedTags = useDebouncedValue(selectedTags, 300);
  const projectsQuery = useProjects();
  const ordering = buildFindingsOrdering(selectedSort, selectedSortDirection);
  const bulkStatusMutation = useBulkFindingStatus();

  const findingsQuery = useFindingsPage(toFindingsApiFilters({
    projectId: selectedProjectId,
    pipelineId: selectedPipelineId,
    createdFrom,
    createdTo,
    statusUpdatedFrom,
    statusUpdatedTo,
    mitigatedFrom,
    mitigatedTo,
    projectVersion: debouncedProjectVersion,
    title: debouncedTitle,
    file: debouncedFile,
    cwe: debouncedCwe,
    severities: selectedSeverities,
    tags: debouncedTags,
    status: selectedStatus,
    risk: selectedRisk,
    aiStatus: selectedAiResponse,
    workItemStatus: selectedWorkItemStatus,
  }, {
    limit: pageSize,
    offset: pageIndex * pageSize,
    ordering,
  }));

  const projects = projectsQuery.data ?? [];
  const aistProjectForFilters = projects.find((project) => project.id === selectedProjectId);
  const findingIds = useMemo(
    () => (findingsQuery.data?.items ?? []).map((finding) => finding.id),
    [findingsQuery.data],
  );
  const aiResponsesQuery = useAiFindingResponses(
    aistProjectForFilters?.id,
    selectedPipelineId,
    findingIds,
  );

  const aiVerdictMap = useMemo(() => {
    const map = new Map<number, string>();
    for (const [findingId, aiResponse] of aiResponsesQuery.data ?? new Map()) {
      if (aiResponse.verdict) {
        map.set(findingId, aiResponse.verdict);
      }
    }
    return map;
  }, [aiResponsesQuery.data]);
  const projectsById = useMemo(
    () => new Map(projects.map((project) => [project.id, project])),
    [projects],
  );

  const findings = useMemo(() => {
    const raw = findingsQuery.data?.items ?? [];
    return raw.map((finding) => {
      const override = findingOverrides[finding.id] ?? {};
      return {
        ...finding,
        ...override,
        product: projectsById.get(finding.projectId ?? 0)?.name ?? finding.product,
        aiVerdict: aiVerdictMap.get(finding.id),
      };
    });
  }, [findingsQuery.data, projectsById, aiVerdictMap, findingOverrides]);

  // When bulk risk accept is selected, use the first visible finding as a
  // product-level proxy to check whether full risk acceptance is enabled.
  // This avoids a dedicated product-level endpoint: findings on the page all
  // belong to the same product when a project filter is active, and it's a
  // best-effort check even without a filter.
  const firstFindingId = bulkAction === "risk_accept" ? findings[0]?.id : undefined;
  const bulkRiskApprovalQuery = useRiskApprovalStatus(firstFindingId);
  const bulkRiskAcceptEnabled = bulkRiskApprovalQuery.data?.enabled ?? true;

  const tagsQuery = useFindingTagsByProject(selectedProjectId);
  const availableTags = tagsQuery.data ?? [];
  const [filterPanelOpen, setFilterPanelOpen] = useState(false);
  const [expandedIds, setExpandedIds] = useState<number[]>([]);
  const pageError = projectsQuery.error ?? findingsQuery.error ?? tagsQuery.error;

  useEffect(() => {
    if (!tagsQuery.isSuccess) return;
    setSelectedTags((current) => current.filter((tag) => availableTags.includes(tag)));
  }, [availableTags, tagsQuery.isSuccess]);

  useEffect(() => {
    if (!expandedIds.length) return;
    const allowed = new Set(findings.map((finding) => finding.id));
    setExpandedIds((current) => current.filter((id) => allowed.has(id)));
  }, [findings, expandedIds.length]);

  useEffect(() => {
    if (!selectedFindingIds.length) return;
    const allowed = new Set(findings.map((finding) => finding.id));
    setSelectedFindingIds((current) => current.filter((id) => allowed.has(id)));
  }, [findings, selectedFindingIds.length]);

  useEffect(() => {
    setPageIndex(0);
  }, [selectedProjectId, selectedSeverities, selectedStatus, selectedRisk, debouncedCwe, debouncedTags, selectedAiResponse, selectedWorkItemStatus, selectedPipelineId, createdFrom, createdTo, statusUpdatedFrom, statusUpdatedTo, mitigatedFrom, mitigatedTo, debouncedFile, debouncedProjectVersion, debouncedTitle, ordering, pageSize]);

  const applyCloseState = (
    findingId: number,
    reason: FindingCloseReason,
  ) => {
    setFindingOverrides((current) => ({
      ...current,
      [findingId]: {
        ...current[findingId],
        active: false,
        isMitigated: reason === "mitigated",
        falsePositive: reason === "false_positive",
        outOfScope: reason === "out_of_scope",
        duplicate: reason === "duplicate",
      },
    }));
  };

  const applyReopenState = (findingId: number) => {
    setFindingOverrides((current) => ({
      ...current,
      [findingId]: {
        ...current[findingId],
        active: true,
        isMitigated: false,
        riskAccepted: false,
        falsePositive: false,
        outOfScope: false,
        duplicate: false,
      },
    }));
  };

  const applySeverityState = (
    findingId: number,
    severity: "Critical" | "High" | "Medium" | "Low" | "Info",
  ) => {
    setFindingOverrides((current) => ({
      ...current,
      [findingId]: {
        ...current[findingId],
        severity,
      },
    }));
  };

  useEffect(() => {
    const parsed = parseFindingsFiltersFromSearch(searchParams);
    setSelectedProjectId(parsed.projectId);
    setSelectedPipelineId(parsed.pipelineId);
    setCreatedFrom(parsed.createdFrom);
    setCreatedTo(parsed.createdTo);
    setStatusUpdatedFrom(parsed.statusUpdatedFrom);
    setStatusUpdatedTo(parsed.statusUpdatedTo);
    setMitigatedFrom(parsed.mitigatedFrom);
    setMitigatedTo(parsed.mitigatedTo);
    setSelectedProjectVersion(parsed.projectVersion);
    setSelectedTitle(parsed.title);
    setSelectedFile(parsed.file);
    setSelectedCwe(parsed.cwe);
    setSelectedSeverities(prev =>
      JSON.stringify(prev) === JSON.stringify(parsed.severities) ? prev : parsed.severities,
    );
    setSelectedTags(prev =>
      JSON.stringify(prev) === JSON.stringify(parsed.tags) ? prev : parsed.tags,
    );
    setSelectedStatus(parsed.status);
    setSelectedRisk(prev =>
      JSON.stringify(prev) === JSON.stringify(parsed.risk) ? prev : parsed.risk,
    );
    setSelectedAiResponse(parsed.aiStatus);
    setSelectedWorkItemStatus(parsed.workItemStatus);
    const rawPage = searchParams.get("page");
    const parsedPage = rawPage ? Number(rawPage) - 1 : 0;
    setPageIndex(Number.isFinite(parsedPage) && parsedPage >= 0 ? parsedPage : 0);
    hasHydratedStatusFromUrl.current = true;
  }, [searchParams]);

  useEffect(() => {
    if (!hasHydratedStatusFromUrl.current) {
      return;
    }
    const filterParams = buildFindingsFilterSearch({
      projectId: selectedProjectId,
      pipelineId: selectedPipelineId,
      createdFrom,
      createdTo,
      statusUpdatedFrom,
      statusUpdatedTo,
      mitigatedFrom,
      mitigatedTo,
      projectVersion: debouncedProjectVersion,
      title: debouncedTitle,
      file: debouncedFile,
      cwe: debouncedCwe,
      severities: selectedSeverities,
      tags: debouncedTags,
      status: selectedStatus,
      risk: selectedRisk,
      aiStatus: selectedAiResponse,
      workItemStatus: selectedWorkItemStatus,
    });
    if (pageIndex > 0) filterParams.set("page", String(pageIndex + 1));
    const nextSearch = filterParams.toString();
    const currentSearch = lastWrittenSearch.current.startsWith("?")
      ? lastWrittenSearch.current.slice(1)
      : lastWrittenSearch.current;
    if (nextSearch === currentSearch) return;
    lastWrittenSearch.current = nextSearch ? `?${nextSearch}` : "";
    navigate(
      {
        pathname: location.pathname,
        search: nextSearch ? `?${nextSearch}` : "",
      },
      { replace: true },
    );
  }, [
    createdFrom,
    createdTo,
    location.pathname,
    mitigatedFrom,
    mitigatedTo,
    navigate,
    pageIndex,
    selectedAiResponse,
    selectedWorkItemStatus,
    debouncedCwe,
    debouncedFile,
    debouncedProjectVersion,
    debouncedTitle,
    debouncedTags,
    selectedPipelineId,
    selectedProjectId,
    selectedRisk,
    selectedSeverities,
    selectedStatus,
    statusUpdatedFrom,
    statusUpdatedTo,
  ]);

  const handleProjectChange = (projectId?: number) => {
    setSelectedProjectId(projectId);
    setSelectedProjectVersion("");
  };
  const clearAllFilters = () => {
    setSelectedProjectId(DEFAULT_FINDINGS_FILTERS.projectId);
    setSelectedSeverities(DEFAULT_FINDINGS_FILTERS.severities);
    setSelectedStatus(DEFAULT_FINDINGS_FILTERS.status);
    setSelectedRisk(DEFAULT_FINDINGS_FILTERS.risk);
    setSelectedAiResponse(DEFAULT_FINDINGS_FILTERS.aiStatus);
    setSelectedWorkItemStatus(DEFAULT_FINDINGS_FILTERS.workItemStatus);
    setSelectedCwe(DEFAULT_FINDINGS_FILTERS.cwe);
    setSelectedFile(DEFAULT_FINDINGS_FILTERS.file);
    setSelectedTitle(DEFAULT_FINDINGS_FILTERS.title);
    setSelectedProjectVersion(DEFAULT_FINDINGS_FILTERS.projectVersion);
    setSelectedTags(DEFAULT_FINDINGS_FILTERS.tags);
    setSelectedPipelineId(DEFAULT_FINDINGS_FILTERS.pipelineId);
    setCreatedFrom(DEFAULT_FINDINGS_FILTERS.createdFrom);
    setCreatedTo(DEFAULT_FINDINGS_FILTERS.createdTo);
    setStatusUpdatedFrom(DEFAULT_FINDINGS_FILTERS.statusUpdatedFrom);
    setStatusUpdatedTo(DEFAULT_FINDINGS_FILTERS.statusUpdatedTo);
    setMitigatedFrom(DEFAULT_FINDINGS_FILTERS.mitigatedFrom);
    setMitigatedTo(DEFAULT_FINDINGS_FILTERS.mitigatedTo);
  };

  const selectedFindingsCount = selectedFindingIds.length;
  const selectedFindingIdsSet = useMemo(() => new Set(selectedFindingIds), [selectedFindingIds]);
  const bulkLockedFindingIdsSet = useMemo(() => new Set(bulkLockedFindingIds), [bulkLockedFindingIds]);
  const selectableVisibleFindingIds = useMemo(
    () => findings.filter((finding) => !bulkLockedFindingIdsSet.has(finding.id)).map((finding) => finding.id),
    [bulkLockedFindingIdsSet, findings],
  );
  const allVisibleSelected = useMemo(
    () => selectableVisibleFindingIds.length > 0
      && selectableVisibleFindingIds.every((id) => selectedFindingIdsSet.has(id)),
    [selectableVisibleFindingIds, selectedFindingIdsSet],
  );
  const canRunBulkAction =
    selectedFindingsCount > 0 &&
    bulkReasonNote.trim().length > 0 &&
    !bulkStatusMutation.isPending &&
    (bulkAction !== "risk_accept" || bulkRiskAcceptEnabled);
  const bulkControlLabelClass = "mb-1 block text-xs uppercase tracking-[0.18em] text-slate-400";
  const toggleBulkFindingSelection = (findingId: number, checked: boolean) => {
    setSelectedFindingIds((current) => {
      if (checked) {
        return current.includes(findingId) ? current : [...current, findingId];
      }
      return current.filter((id) => id !== findingId);
    });
  };
  const toggleBulkEditMode = () => {
    setBulkEditMode((current) => {
      const next = !current;
      if (!next) {
        setSelectedFindingIds([]);
        setBulkReasonNote("");
      }
      return next;
    });
  };
  const toggleSelectAllVisible = (checked: boolean) => {
    setSelectedFindingIds((current) => {
      const next = new Set(current);
      for (const findingId of selectableVisibleFindingIds) {
        if (checked) next.add(findingId);
        else next.delete(findingId);
      }
      return [...next];
    });
  };

  const applyBulkStatus = () => {
    const findingIds = [...selectedFindingIds];
    if (!findingIds.length) {
      toast.push("Select at least one finding.", "error");
      return;
    }
    const reason = bulkReasonNote.trim();
    if (!reason) {
      toast.push("Reason is required.", "error");
      return;
    }
    // Capture mutable state values at call-time so async callbacks are not
    // affected by state changes that may occur while the request is in-flight.
    const capturedAction = bulkAction;
    const capturedCloseReason = bulkCloseReason;

    setBulkLockedFindingIds(findingIds);
    bulkStatusMutation.mutate(
      {
        findingIds,
        action: capturedAction,
        reason,
        closeReason: capturedAction === "close" ? capturedCloseReason : undefined,
      },
      {
        onSuccess: (result) => {
          const changedIds = new Set(result.updated_ids);
          if (capturedAction === "reopen") {
            setFindingOverrides((current) => {
              const next = { ...current };
              for (const findingId of changedIds) {
                next[findingId] = {
                  ...next[findingId],
                  active: true,
                  isMitigated: false,
                  falsePositive: false,
                  outOfScope: false,
                  duplicate: false,
                };
              }
              return next;
            });
          } else if (capturedAction === "risk_accept") {
            setFindingOverrides((current) => {
              const next = { ...current };
              for (const findingId of changedIds) {
                next[findingId] = {
                  ...next[findingId],
                  active: false,
                  riskAccepted: true,
                  // Clear any prior close-reason flags so the card displays
                  // "Risk Accepted" rather than a stale mitigated/FP/etc. state.
                  isMitigated: false,
                  falsePositive: false,
                  outOfScope: false,
                  duplicate: false,
                };
              }
              return next;
            });
          } else {
            setFindingOverrides((current) => {
              const next = { ...current };
              for (const findingId of changedIds) {
                next[findingId] = {
                  ...next[findingId],
                  active: false,
                  isMitigated: capturedCloseReason === "mitigated",
                  falsePositive: capturedCloseReason === "false_positive",
                  outOfScope: capturedCloseReason === "out_of_scope",
                  duplicate: capturedCloseReason === "duplicate",
                };
              }
              return next;
            });
          }
          setSelectedFindingIds([]);
          setBulkReasonNote("");
          toast.push(`Updated ${result.updated_count} findings.`, "success");
        },
        onError: (error) => {
          if (error instanceof ApiError && error.status === 423) {
            const payload = (error.payload ?? {}) as { locked_ids?: number[] };
            // Keep the locked highlight so the user can see which findings
            // are blocked. onSettled will NOT clear it in this case.
            setBulkLockedFindingIds(payload.locked_ids ?? findingIds);
          } else {
            setBulkLockedFindingIds([]);
          }
          toast.push(`Bulk update failed: ${toUserMessage(error)}`, "error");
        },
        onSettled: (_, error) => {
          // On 423 the locked highlight is intentionally kept — cleared by
          // the user when they cancel bulk edit or retry successfully.
          if (!(error instanceof ApiError && error.status === 423)) {
            setBulkLockedFindingIds([]);
          }
        },
      },
    );
  };

  const exportCurrentView = () => {
    if (findings.length === 0) {
      toast.push("No findings to export.", "error");
      return;
    }
    const headers = ["id", "title", "severity", "status", "product", "filePath", "line", "date", "aiVerdict"];
    const rows = findings.map((finding) => [
      finding.id,
      finding.title,
      finding.severity,
      finding.active ? "enabled" : "disabled",
      finding.product,
      finding.filePath,
      finding.line,
      finding.date ?? "",
      finding.aiVerdict ?? "",
    ]);
    const csv = [headers, ...rows]
      .map((row) => row.map((value) => `"${String(value).replace(/"/g, '""').replace(/\r?\n/g, " ")}"`).join(","))
      .join("\r\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const url = window.URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "findings-export.csv";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.URL.revokeObjectURL(url);
    toast.push("Exported current view.", "success");
  };

  if (projectsQuery.isLoading) {
    return <SkeletonBlock />;
  }

  if (pageError) {
    return <PageErrorState error={pageError} fallbackTitle="Failed to load findings" />;
  }

  return (
    <div className="grid min-h-0 gap-6 lg:grid-cols-[280px_minmax(0,1fr)]">
      <div className={["aist-scrollbar lg:sticky lg:top-24 self-start max-h-[calc(100vh-140px)] overflow-auto", filterPanelOpen ? "" : "hidden lg:block"].join(" ").trim()}>
        <FilterPanel
          products={projects}
          selectedProjectId={selectedProjectId}
          onProjectChange={handleProjectChange}
          selectedSeverities={selectedSeverities}
          onSeveritiesChange={setSelectedSeverities}
          selectedFile={selectedFile}
          onFileChange={setSelectedFile}
          selectedTitle={selectedTitle}
          onTitleChange={setSelectedTitle}
          createdFrom={createdFrom}
          onCreatedFromChange={setCreatedFrom}
          createdTo={createdTo}
          onCreatedToChange={setCreatedTo}
          selectedProjectVersion={selectedProjectVersion}
          onProjectVersionChange={(value) => {
            setSelectedProjectVersion(value);
          }}
          selectedStatus={selectedStatus}
          onStatusChange={(value) => setSelectedStatus(toFindingStatusFilter(value))}
          selectedRisk={selectedRisk}
          onRiskChange={setSelectedRisk}
          selectedCwe={selectedCwe}
          onCweChange={setSelectedCwe}
          availableTags={availableTags}
          selectedTags={selectedTags}
          onTagsChange={setSelectedTags}
          selectedAiResponse={selectedAiResponse}
          onAiResponseChange={setSelectedAiResponse}
          selectedWorkItemStatus={selectedWorkItemStatus}
          onWorkItemStatusChange={setSelectedWorkItemStatus}
          onClearAll={clearAllFilters}
        />
      </div>

      <div className="flex min-h-0 min-w-0 flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3 text-xs uppercase tracking-[0.2em] text-slate-400">
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="aist-icon-button h-8 px-2 text-xs lg:hidden"
              aria-label={filterPanelOpen ? "Hide filters" : "Show filters"}
              onClick={() => setFilterPanelOpen((open) => !open)}
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
                <path fill="currentColor" d="M10 18h4v-2h-4v2Zm-7-10v2h18V8H3Zm3 7h12v-2H6v2Z" />
              </svg>
              Filters
            </button>
            <span className="flex items-center gap-1.5">
              Findings · Total {findingsQuery.data?.count ?? 0}
              {findingsQuery.isFetching && !findingsQuery.isLoading ? (
                <svg
                  className="h-3.5 w-3.5 animate-spin text-brand-400"
                  viewBox="0 0 24 24"
                  fill="none"
                  aria-label="Refreshing"
                >
                  <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" strokeDasharray="40 20" strokeLinecap="round" />
                </svg>
              ) : null}
            </span>
          </div>
          <div className="flex w-full flex-col items-stretch gap-2 sm:w-auto sm:flex-row sm:items-end">
            <PermissionGate action="write">
              <button
                className="aist-icon-button h-10 w-full sm:w-auto text-xs font-semibold uppercase tracking-[0.14em]"
                onClick={toggleBulkEditMode}
                disabled={bulkStatusMutation.isPending}
              >
                <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
                  {bulkEditMode ? (
                    <path
                      fill="currentColor"
                      d="m7.41 6 4.59 4.59L16.59 6 18 7.41 13.41 12 18 16.59 16.59 18 12 13.41 7.41 18 6 16.59 10.59 12 6 7.41 7.41 6Z"
                    />
                  ) : (
                    <path
                      fill="currentColor"
                      d="M11 5h2v6h6v2h-6v6h-2v-6H5v-2h6V5Z"
                    />
                  )}
                </svg>
                {bulkEditMode ? "Cancel Bulk Edit" : "Start Bulk Edit"}
              </button>
            </PermissionGate>
            <button
              className="aist-icon-button h-10 w-full sm:w-auto"
              onClick={exportCurrentView}
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
                <path
                  fill="currentColor"
                  d="M12 3l4 4h-3v6h-2V7H8l4-4Zm-7 12h14v4a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-4Zm3 2v2h8v-2H8Z"
                />
              </svg>
              Export current view
            </button>
            <SegmentedSortControl
              options={FINDINGS_SORT_OPTIONS}
              value={selectedSort}
              direction={selectedSortDirection}
              onValueChange={setSelectedSort}
              onDirectionToggle={() =>
                setSelectedSortDirection((current) => (current === "desc" ? "asc" : "desc"))
              }
            />
          </div>
        </div>
        {bulkEditMode ? (
          <div className="rounded-2xl border border-night-500 bg-night-700/95 p-4 shadow-panel">
            <div className="mb-3 flex items-center justify-between gap-2 text-xs uppercase tracking-[0.18em] text-slate-400">
              <span>Bulk Edit</span>
              <span>Selected: {selectedFindingsCount}</span>
            </div>
            <div className="flex flex-wrap items-end gap-3">
              <div className="min-w-[180px] flex-1">
                <label className={bulkControlLabelClass}>BULK ACTION</label>
                <SelectField
                  label="BULK ACTION"
                  value={bulkAction}
                  onChange={(value) => setBulkAction(value as "close" | "reopen" | "risk_accept")}
                  options={BULK_ACTION_OPTIONS}
                  hideLabel
                />
              </div>
              {bulkAction === "close" ? (
                <div className="min-w-[220px] flex-1">
                  <label className={bulkControlLabelClass}>CLOSE AS</label>
                  <SelectField
                    label="CLOSE AS"
                    value={bulkCloseReason}
                    onChange={(value) => setBulkCloseReason(value as FindingCloseReason)}
                    options={BULK_CLOSE_REASON_OPTIONS}
                    hideLabel
                  />
                </div>
              ) : null}
              <div className="min-w-[260px] flex-[2]">
                <label className={bulkControlLabelClass}>
                  {bulkAction === "risk_accept" ? "JUSTIFICATION" : "REASON"}
                </label>
                <TextInput
                  value={bulkReasonNote}
                  onChange={(event) => setBulkReasonNote(event.target.value)}
                  placeholder={bulkAction === "risk_accept" ? "Enter risk justification" : "Enter reason for audit log"}
                />
              </div>
              {bulkAction === "risk_accept" ? (
                <div className="w-full">
                  {!bulkRiskAcceptEnabled ? (
                    <div className="flex items-center gap-2 rounded-xl border border-danger-500/30 bg-danger-500/8 px-3 py-2 text-xs text-danger-300">
                      <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M12 3 5 6v6c0 4.2 2.8 8.1 7 9 4.2-.9 7-4.8 7-9V6l-7-3Z" />
                        <path d="M12 8v4M12 16h.01" />
                      </svg>
                      Full risk acceptance is not enabled for this product. Contact your product owner to enable it.
                    </div>
                  ) : (
                    <div className="flex items-center gap-2 rounded-xl border border-amber-400/30 bg-amber-400/8 px-3 py-2 text-xs text-amber-300">
                      <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M12 3 5 6v6c0 4.2 2.8 8.1 7 9 4.2-.9 7-4.8 7-9V6l-7-3Z" />
                        <path d="m9.5 12 1.7 1.7 3.3-3.4" />
                      </svg>
                      Risk Accept is a compliance decision recorded under your name. Ensure the justification meets your policy requirements before applying.
                    </div>
                  )}
                </div>
              ) : null}
              <button
                type="button"
                className="aist-icon-button h-10 text-xs font-semibold uppercase tracking-[0.14em] disabled:opacity-50"
                disabled={selectableVisibleFindingIds.length === 0 || bulkStatusMutation.isPending}
                onClick={() => toggleSelectAllVisible(!allVisibleSelected)}
              >
                {allVisibleSelected
                  ? `Deselect Visible (${selectableVisibleFindingIds.length})`
                  : `Select Visible (${selectableVisibleFindingIds.length})`}
              </button>
              <button
                className="aist-icon-button h-10 text-xs font-semibold uppercase tracking-[0.14em] disabled:opacity-50"
                disabled={selectedFindingsCount === 0 || bulkStatusMutation.isPending}
                onClick={() => setSelectedFindingIds([])}
              >
                Clear
              </button>
              <button
                className="aist-icon-button h-10 text-xs font-semibold uppercase tracking-[0.14em] disabled:opacity-50"
                disabled={bulkStatusMutation.isPending}
                onClick={toggleBulkEditMode}
              >
                Hide Bulk Edit
              </button>
              <button
                className="h-10 rounded-xl bg-brand-500 px-4 text-xs font-semibold uppercase tracking-[0.14em] text-night-900 disabled:opacity-50"
                disabled={!canRunBulkAction}
                onClick={applyBulkStatus}
              >
                Apply to {selectedFindingsCount}
              </button>
            </div>
          </div>
        ) : null}
        <div className="flex min-h-[calc(100vh-280px)] flex-col">
          {findingsQuery.isLoading ? (
            <div className="space-y-4">
              <SkeletonBlock />
              <SkeletonBlock />
              <SkeletonBlock />
            </div>
          ) : findings.length === 0 ? (
            <div className="flex flex-col items-center gap-4 rounded-2xl border border-night-500 bg-night-700 px-6 py-12 text-center">
              <svg viewBox="0 0 24 24" className="h-10 w-10 text-slate-600" aria-hidden="true">
                <path fill="currentColor" d="M9.5 3A6.5 6.5 0 0 1 16 9.5c0 1.61-.59 3.09-1.56 4.23l.27.27h.79l5 5-1.5 1.5-5-5v-.79l-.27-.27A6.516 6.516 0 0 1 9.5 16 6.5 6.5 0 0 1 3 9.5 6.5 6.5 0 0 1 9.5 3m0 2C7 5 5 7 5 9.5S7 14 9.5 14 14 12 14 9.5 12 5 9.5 5Z" />
              </svg>
              <div>
                <p className="text-sm font-medium text-slate-300">No findings match the current filters</p>
                <p className="mt-1 text-xs text-slate-500">Try adjusting or clearing your filters to see more results.</p>
              </div>
              <button
                type="button"
                className="aist-icon-button h-9 px-4 text-xs font-semibold uppercase tracking-[0.14em]"
                onClick={clearAllFilters}
              >
                Clear filters
              </button>
            </div>
          ) : (
            <div className="min-h-0 flex-1 pr-2">
              <div className="space-y-4">
                {findings.map((finding) => {
                  const aiResponse = (aiResponsesQuery.data ?? new Map()).get(finding.id) ?? null;
                  return (
                    <div key={finding.id} className="space-y-3">
                      <FindingCard
                        finding={finding}
                        showBulkSelection={bulkEditMode}
                        selectedForBulk={selectedFindingIdsSet.has(finding.id)}
                        onToggleBulkSelection={toggleBulkFindingSelection}
                        bulkLocked={bulkLockedFindingIdsSet.has(finding.id)}
                        isOpen={expandedIds.includes(finding.id)}
                        onSelectProjectVersion={(projectVersion) => {
                          setSelectedProjectVersion(projectVersion);
                          if (finding.projectId) {
                            setSelectedProjectId(finding.projectId);
                          }
                        }}
                        onSelectProject={(projectId) => {
                          setSelectedProjectId(projectId);
                          setSelectedPipelineId(undefined);
                        }}
                        onSelectFile={setSelectedFile}
                        expandedContent={
                          expandedIds.includes(finding.id) ? (
                            <Suspense
                              fallback={(
                                <div className="rounded-xl border border-night-500 bg-night-900 px-4 py-3 text-sm text-slate-400">
                                  Loading detail...
                                </div>
                              )}
                            >
                              <DetailPanel
                                finding={finding}
                                permissionProductId={projectsById.get(finding.projectId ?? 0)?.productId}
                                aiResponse={aiResponse}
                                selectedTags={selectedTags}
                                onToggleTag={(tag) =>
                                  setSelectedTags((current) =>
                                    current.includes(tag)
                                      ? current.filter((item) => item !== tag)
                                      : [...current, tag],
                                  )
                                }
                                selectedCwe={selectedCwe}
                                onToggleCwe={(cwe) =>
                                  setSelectedCwe((current) => (current === cwe ? "" : cwe))
                                }
                                onCloseApplied={applyCloseState}
                                onReopened={applyReopenState}
                                onSeverityChanged={applySeverityState}
                                isStatusEditLocked={bulkLockedFindingIdsSet.has(finding.id)}
                                embedded
                              />
                            </Suspense>
                          ) : null
                        }
                        onSelect={() =>
                          setExpandedIds((current) => {
                            if (current.includes(finding.id)) {
                              return current.filter((id) => id !== finding.id);
                            }
                            const next = [...current, finding.id];
                            if (next.length > 3) {
                              next.shift();
                            }
                            return next;
                          })
                        }
                      />
                    </div>
                  );
                })}
              </div>
            </div>
          )}
          {findingsQuery.data ? (
            <div className="mt-auto">
              <PaginationBar
                count={findingsQuery.data.count}
                noun="findings"
                pageIndex={pageIndex}
                pageSize={pageSize}
                onPageIndexChange={setPageIndex}
                onPageSizeChange={setPageSize}
              />
            </div>
          ) : null}
        </div>
      </div>

    </div>
  );
}
