import { Suspense, lazy, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import FilterPanel from "../components/FilterPanel";
import FindingCard from "../components/FindingCard";
import SegmentedSortControl from "../components/SegmentedSortControl";
import type { Finding, RiskState, Severity } from "../types";
import {
  useAiFindingResponses,
  useFindingTagsByProject,
  useFindingsPage,
  useProjects,
} from "../lib/queries";
import { useToast } from "../components/ToastProvider";
import PaginationBar from "../components/PaginationBar";
import { buildFindingsOrdering, FINDINGS_SORT_OPTIONS, type FindingsSortKey } from "../lib/findingsSort";
import PageErrorState from "../components/PageErrorState";

const DetailPanel = lazy(() => import("../components/DetailPanel"));

export default function FindingsPage() {
  const [selectedProjectId, setSelectedProjectId] = useState<number | undefined>();
  const [selectedSeverities, setSelectedSeverities] = useState<Severity[]>([]);
  const [selectedStatus, setSelectedStatus] = useState<string>("All");
  const [selectedRisk, setSelectedRisk] = useState<RiskState[]>([]);
  const [selectedAiResponse, setSelectedAiResponse] = useState<string>("All");
  const [selectedSort, setSelectedSort] = useState<FindingsSortKey>("severity");
  const [selectedSortDirection, setSelectedSortDirection] = useState<"asc" | "desc">("desc");
  const [selectedCwe, setSelectedCwe] = useState<string>("");
  const [selectedFile, setSelectedFile] = useState<string>("");
  const [selectedProjectVersion, setSelectedProjectVersion] = useState<string>("");
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [selectedPipelineId, setSelectedPipelineId] = useState<string | undefined>();
  const [findingOverrides, setFindingOverrides] = useState<Record<number, Partial<Finding>>>({});
  const toast = useToast();
  const [searchParams] = useSearchParams();
  const [pageSize, setPageSize] = useState<number>(50);
  const [pageIndex, setPageIndex] = useState<number>(0);
  const projectsQuery = useProjects();
  const ordering = buildFindingsOrdering(selectedSort, selectedSortDirection);

  const findingsQuery = useFindingsPage({
    projectId: selectedProjectId,
    pipelineId: selectedPipelineId,
    aiStatus:
      selectedAiResponse === "All"
        ? undefined
        : (selectedAiResponse as "has_ai" | "no_ai" | "ai_tp" | "ai_fp" | "ai_u"),
    projectVersion: selectedProjectVersion || undefined,
    file: selectedFile || undefined,
    severities: selectedSeverities.length ? selectedSeverities : undefined,
    status: selectedStatus === "Active" ? "enabled" : selectedStatus === "Non-Active" ? "disabled" : undefined,
    riskStates: selectedRisk.length ? selectedRisk : undefined,
    cwe: selectedCwe ? selectedCwe : undefined,
    tags: selectedTags.length ? selectedTags : undefined,
    limit: pageSize,
    offset: pageIndex * pageSize,
    ordering,
  });

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

  const tagsQuery = useFindingTagsByProject(selectedProjectId);
  const availableTags = tagsQuery.data ?? [];
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
    setPageIndex(0);
  }, [selectedProjectId, selectedSeverities, selectedStatus, selectedRisk, selectedCwe, selectedTags, selectedAiResponse, selectedPipelineId, selectedFile, selectedProjectVersion, ordering, pageSize]);

  const applyCloseState = (
    findingId: number,
    reason: "mitigated" | "false_positive" | "out_of_scope" | "duplicate",
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
        falsePositive: false,
        outOfScope: false,
        duplicate: false,
      },
    }));
  };

  useEffect(() => {
    const projectRaw = searchParams.get("project") ?? searchParams.get("project_id");
    const projectParsed = projectRaw ? Number(projectRaw) : NaN;
    setSelectedProjectId(Number.isFinite(projectParsed) ? projectParsed : undefined);

    const pipeline = searchParams.get("pipeline") ?? searchParams.get("pipeline_id");
    setSelectedPipelineId(pipeline || undefined);
    setSelectedProjectVersion(searchParams.get("project_version") ?? "");
    setSelectedFile(searchParams.get("file") ?? "");
    setSelectedCwe(searchParams.get("cwe") ?? "");

    const severities = (searchParams.get("severity") ?? "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean) as Severity[];
    setSelectedSeverities(severities);

    const tags = (searchParams.get("tags") ?? "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    setSelectedTags(tags);

    const active = (searchParams.get("active") ?? "").toLowerCase();
    if (active === "true" || active === "1") {
      setSelectedStatus("Active");
    } else if (active === "false" || active === "0") {
      setSelectedStatus("Non-Active");
    } else {
      const status = (searchParams.get("status") ?? "").toLowerCase();
      if (status === "active" || status === "enabled") {
        setSelectedStatus("Active");
      } else if (status === "non-active" || status === "disabled") {
        setSelectedStatus("Non-Active");
      } else {
        setSelectedStatus("All");
      }
    }

    const nextRisk: RiskState[] = [];
    const riskAccepted = (searchParams.get("risk_accepted") ?? "").toLowerCase();
    const underReview = (searchParams.get("under_review") ?? "").toLowerCase();
    const isMitigated = (searchParams.get("is_mitigated") ?? "").toLowerCase();
    if (riskAccepted === "true" || riskAccepted === "1") nextRisk.push("risk_accepted");
    if (underReview === "true" || underReview === "1") nextRisk.push("under_review");
    if (isMitigated === "true" || isMitigated === "1") nextRisk.push("mitigated");
    setSelectedRisk(nextRisk);

    setSelectedAiResponse(searchParams.get("ai_status") || "All");
  }, [searchParams]);

  const handleProjectChange = (projectId?: number) => {
    setSelectedProjectId(projectId);
    setSelectedProjectVersion("");
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
      .map((row) => row.map((value) => `"${String(value).replace(/\"/g, '""')}"`).join(","))
      .join("\n");
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
    return (
      <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
        Loading findings...
      </div>
    );
  }

  if (pageError) {
    return <PageErrorState error={pageError} fallbackTitle="Failed to load findings" />;
  }

  return (
    <div className="grid min-h-0 gap-6 lg:grid-cols-[280px_minmax(0,1fr)]">
      <div className="aist-scrollbar lg:sticky lg:top-24 self-start max-h-[calc(100vh-140px)] overflow-auto">
        <FilterPanel
          products={projects}
          selectedProjectId={selectedProjectId}
          onProjectChange={handleProjectChange}
          selectedSeverities={selectedSeverities}
          onSeveritiesChange={setSelectedSeverities}
          selectedFile={selectedFile}
          onFileChange={setSelectedFile}
          selectedProjectVersion={selectedProjectVersion}
          onProjectVersionChange={(value) => {
            setSelectedProjectVersion(value);
          }}
          selectedStatus={selectedStatus}
          onStatusChange={setSelectedStatus}
          selectedRisk={selectedRisk}
          onRiskChange={setSelectedRisk}
          selectedCwe={selectedCwe}
          onCweChange={setSelectedCwe}
          availableTags={availableTags}
          selectedTags={selectedTags}
          onTagsChange={setSelectedTags}
          selectedAiResponse={selectedAiResponse}
          onAiResponseChange={setSelectedAiResponse}
        />
      </div>

      <div className="flex min-h-0 min-w-0 flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3 text-xs uppercase tracking-[0.2em] text-slate-400">
          <span>Findings · Total {findingsQuery.data?.count ?? 0}</span>
          <div className="flex w-full flex-col items-stretch gap-2 sm:w-auto sm:flex-row sm:items-end">
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
        <div className="flex min-h-[calc(100vh-280px)] flex-col">
          {findingsQuery.isLoading ? (
            <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
              Loading findings...
            </div>
          ) : findings.length === 0 ? (
            <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
              No findings match the current filters.
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
                                pipelineId={selectedPipelineId ?? aiResponse?.pipelineId}
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
