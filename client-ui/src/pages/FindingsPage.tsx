import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import FilterPanel from "../components/FilterPanel";
import FindingCard from "../components/FindingCard";
import DetailPanel from "../components/DetailPanel";
import type { Finding } from "../types";
import {
  useAiResponse,
  useFindingTagsByProduct,
  useFindingsPage,
  usePipelines,
  useProjectMeta,
  useProjects,
} from "../lib/queries";
import { useToast } from "../components/ToastProvider";
import SelectField from "../components/SelectField";
import PaginationBar from "../components/PaginationBar";

export default function FindingsPage() {
  const [selectedProductId, setSelectedProductId] = useState<number | undefined>();
  const [selectedSeverity, setSelectedSeverity] = useState<string>("All severities");
  const [selectedStatus, setSelectedStatus] = useState<string>("All");
  const [selectedRisk, setSelectedRisk] = useState<string[]>([]);
  const [selectedAiVerdict, setSelectedAiVerdict] = useState<string>("All");
  const [selectedSort, setSelectedSort] = useState<string>("severity");
  const [selectedCwe, setSelectedCwe] = useState<string>("");
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [selectedPipelineId, setSelectedPipelineId] = useState<string | undefined>();
  const toast = useToast();
  const [searchParams] = useSearchParams();
  const [pageSize, setPageSize] = useState<number>(50);
  const [pageIndex, setPageIndex] = useState<number>(0);
  const projectsQuery = useProjects();
  const ordering =
    selectedSort === "severity"
      ? "numerical_severity"
      : selectedSort === "date"
        ? "-date"
        : "title";

  const findingsQuery = useFindingsPage({
    productId: selectedProductId,
    pipelineId: selectedPipelineId,
    severity: selectedSeverity !== "All severities" && selectedSeverity !== "All" ? (selectedSeverity as any) : undefined,
    status: selectedStatus === "Active" ? "enabled" : selectedStatus === "Non-Active" ? "disabled" : undefined,
    riskStates: selectedRisk.length ? (selectedRisk as any) : undefined,
    cwe: selectedCwe ? selectedCwe : undefined,
    tags: selectedTags.length ? selectedTags : undefined,
    limit: pageSize,
    offset: pageIndex * pageSize,
    ordering,
  });

  const projects = projectsQuery.data ?? [];
  const projectIdByProduct = useMemo(
    () => new Map(projects.map((project) => [project.productId, project.id])),
    [projects],
  );
  const aistProjectForFilters = projects.find((project) => project.productId === selectedProductId);
  const pipelinesQuery = usePipelines(aistProjectForFilters?.id);
  const filterMetaQuery = useProjectMeta(aistProjectForFilters?.id);
  const filterProjectVersionId = filterMetaQuery.data?.versions?.length
    ? Number(filterMetaQuery.data.versions[filterMetaQuery.data.versions.length - 1].id)
    : undefined;

  const aiVerdictMap = useMemo(() => {
    const map = new Map<number, string>();
    for (const pipeline of pipelinesQuery.data ?? []) {
      const response = pipeline.response_from_ai;
      if (!response?.results) continue;
      const pools = [
        ...(response.results.true_positives ?? []).map((entry: any) => ({
          id: entry?.originalFinding?.id,
          verdict: "true_positive",
        })),
        ...(response.results.false_positives ?? []).map((entry: any) => ({
          id: entry?.originalFinding?.id,
          verdict: "false_positive",
        })),
        ...(response.results.uncertainly ?? []).map((entry: any) => ({
          id: entry?.originalFinding?.id,
          verdict: "uncertain",
        })),
      ];
      for (const item of pools) {
        if (item.id) {
          map.set(item.id, item.verdict);
        }
      }
    }
    return map;
  }, [pipelinesQuery.data]);

  const findings = useMemo(() => {
    const raw = findingsQuery.data?.items ?? [];
    const productMap = new Map(projects.map((project) => [project.productId, project.name]));
    let mapped = raw.map((finding) => ({
      ...finding,
      product: productMap.get(finding.productId ?? 0) ?? finding.product,
      aiVerdict: aiVerdictMap.get(finding.id) as any,
    }));
    if (selectedAiVerdict !== "All") {
      mapped = mapped.filter((finding) => finding.aiVerdict === selectedAiVerdict);
    }
    return mapped;
  }, [findingsQuery.data, projects, aiVerdictMap, selectedAiVerdict]);

  const tagsQuery = useFindingTagsByProduct(selectedProductId);
  const availableTags = tagsQuery.data ?? [];
  const [expandedIds, setExpandedIds] = useState<number[]>([]);

  useEffect(() => {
    setSelectedTags([]);
  }, [selectedProductId]);

  useEffect(() => {
    setSelectedTags((current) => current.filter((tag) => availableTags.includes(tag)));
  }, [availableTags]);

  useEffect(() => {
    if (!expandedIds.length) return;
    const allowed = new Set(findings.map((finding) => finding.id));
    setExpandedIds((current) => current.filter((id) => allowed.has(id)));
  }, [findings, expandedIds.length]);

  const metaQuery = useProjectMeta(aistProjectForFilters?.id);
  const projectVersionId = metaQuery.data?.versions?.length
    ? Number(metaQuery.data.versions[metaQuery.data.versions.length - 1].id)
    : undefined;

  useEffect(() => {
    setPageIndex(0);
  }, [selectedProductId, selectedSeverity, selectedStatus, selectedRisk, selectedCwe, selectedTags, selectedAiVerdict, selectedPipelineId, ordering, pageSize]);

  useEffect(() => {
    const productParam = searchParams.get("product");
    const pipelineParam = searchParams.get("pipeline");
    if (productParam) {
      const parsed = Number(productParam);
      if (!Number.isNaN(parsed)) {
        setSelectedProductId(parsed);
      }
    }
    if (pipelineParam) {
      setSelectedPipelineId(pipelineParam);
    }
  }, [searchParams]);

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

  return (
    <div className="grid min-h-0 gap-6 lg:grid-cols-[280px_minmax(0,1fr)]">
      <div className="lg:sticky lg:top-24 self-start max-h-[calc(100vh-140px)] overflow-auto">
        <FilterPanel
          products={projects}
          selectedProductId={selectedProductId}
          onProductChange={setSelectedProductId}
          selectedSeverity={selectedSeverity}
          onSeverityChange={setSelectedSeverity}
          selectedStatus={selectedStatus}
          onStatusChange={setSelectedStatus}
          selectedRisk={selectedRisk}
          onRiskChange={setSelectedRisk}
          selectedCwe={selectedCwe}
          onCweChange={setSelectedCwe}
          availableTags={availableTags}
          selectedTags={selectedTags}
          onTagsChange={setSelectedTags}
          selectedAiVerdict={selectedAiVerdict}
          onAiVerdictChange={setSelectedAiVerdict}
          aiVerdictDisabled={!selectedProductId}
        />
      </div>

      <div className="flex min-h-0 min-w-0 flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3 text-xs uppercase tracking-[0.2em] text-slate-400">
          <span>Findings</span>
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
            <div className="w-full sm:w-44">
              <SelectField
                label="Sort"
                value={selectedSort}
                onChange={setSelectedSort}
                hideLabel
                options={[
                  { value: "severity", label: "Sort: Severity" },
                  { value: "date", label: "Sort: Date" },
                  { value: "title", label: "Sort: Title" },
                ]}
              />
            </div>
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
                  const aiResponse = useAiResponse(pipelinesQuery.data ?? [], finding.id);
                  return (
                    <div key={finding.id} className="space-y-3">
                      <FindingCard
                        finding={finding}
                        projectId={projectIdByProduct.get(finding.productId ?? 0)}
                        projectVersionId={selectedProductId ? filterProjectVersionId : undefined}
                        isOpen={expandedIds.includes(finding.id)}
                        selectedTags={selectedTags}
                        onToggleTag={(tag) =>
                          setSelectedTags((current) =>
                            current.includes(tag)
                              ? current.filter((item) => item !== tag)
                              : [...current, tag],
                          )
                        }
                        expandedContent={
                          expandedIds.includes(finding.id) ? (
                            <DetailPanel
                              finding={{
                                ...finding,
                                projectVersionId:
                                  selectedProductId && selectedProductId === finding.productId
                                    ? filterProjectVersionId
                                    : projectVersionId,
                              }}
                              aiResponse={aiResponse}
                              pipelineId={aiResponse?.pipelineId}
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
                              embedded
                            />
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
