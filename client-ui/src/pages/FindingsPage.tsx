import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import FilterPanel from "../components/FilterPanel";
import FindingCard from "../components/FindingCard";
import DetailPanel from "../components/DetailPanel";
import type { Finding } from "../types";
import {
  useAiResponse,
  useFindingTagsByProduct,
  useFindingsWithFilters,
  usePipelines,
  useProjectMeta,
  useProjects,
} from "../lib/queries";
import { useToast } from "../components/ToastProvider";
import SelectField from "../components/SelectField";

export default function FindingsPage() {
  const [selectedProductId, setSelectedProductId] = useState<number | undefined>();
  const [selectedSeverity, setSelectedSeverity] = useState<string>("All severities");
  const [selectedStatus, setSelectedStatus] = useState<string>("All");
  const [selectedRisk, setSelectedRisk] = useState<string[]>([]);
  const [selectedAiVerdict, setSelectedAiVerdict] = useState<string>("All");
  const [selectedSort, setSelectedSort] = useState<string>("severity");
  const [selectedCwe, setSelectedCwe] = useState<string>("");
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const toast = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const pageSize = 50;
  const projectsQuery = useProjects();
  const ordering =
    selectedSort === "severity"
      ? "numerical_severity"
      : selectedSort === "date"
        ? "-date"
        : "title";

  const findingsQuery = useFindingsWithFilters({
    productId: selectedProductId,
    severity: selectedSeverity !== "All severities" && selectedSeverity !== "All" ? (selectedSeverity as any) : undefined,
    status: selectedStatus === "Active" ? "enabled" : selectedStatus === "Non-Active" ? "disabled" : undefined,
    riskStates: selectedRisk.length ? (selectedRisk as any) : undefined,
    cwe: selectedCwe ? selectedCwe : undefined,
    tags: selectedTags.length ? selectedTags : undefined,
    limit: pageSize,
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
    const raw = findingsQuery.data?.pages.flatMap((page) => page.items) ?? [];
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

  useEffect(() => {
    setSelectedTags([]);
  }, [selectedProductId]);

  useEffect(() => {
    setSelectedTags((current) => current.filter((tag) => availableTags.includes(tag)));
  }, [availableTags]);

  const [selected, setSelected] = useState<Finding | undefined>(findings[0]);
  const parentRef = useRef<HTMLDivElement | null>(null);

  const aistProjectForSelected = projects.find((project) => project.productId === selected?.productId);
  const selectedPipelinesQuery = usePipelines(aistProjectForSelected?.id);
  const aiResponse = useAiResponse(selectedPipelinesQuery.data ?? [], selected?.id);

  const metaQuery = useProjectMeta(aistProjectForSelected?.id);
  const projectVersionId = metaQuery.data?.versions?.length
    ? Number(metaQuery.data.versions[metaQuery.data.versions.length - 1].id)
    : undefined;

  useEffect(() => {
    const el = parentRef.current;
    if (!el) return;
    const onScroll = () => {
      if (!findingsQuery.hasNextPage || findingsQuery.isFetchingNextPage) return;
      if (el.scrollTop + el.clientHeight >= el.scrollHeight - 400) {
        findingsQuery.fetchNextPage();
      }
    };
    el.addEventListener("scroll", onScroll);
    return () => el.removeEventListener("scroll", onScroll);
  }, [findingsQuery]);

  useEffect(() => {
    const productParam = searchParams.get("product");
    if (!productParam) return;
    const parsed = Number(productParam);
    if (!Number.isNaN(parsed)) {
      setSelectedProductId(parsed);
    }
    setSearchParams(
      (params) => {
        params.delete("product");
        return params;
      },
      { replace: true },
    );
  }, [searchParams, setSearchParams]);

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
    <div className="grid min-h-0 gap-6 lg:grid-cols-[280px_minmax(0,1fr)_360px]">
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

      <div className="flex min-h-0 min-w-0 flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs uppercase tracking-[0.2em] text-slate-400">
          <span>Findings</span>
          <div className="flex items-center gap-2">
            <button
              className="rounded-xl border border-night-500 bg-night-700 px-3 py-2 text-xs text-slate-200"
              onClick={exportCurrentView}
            >
              Export current view
            </button>
            <div className="w-44">
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
        {findingsQuery.isLoading ? (
          <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
            Loading findings...
          </div>
        ) : findings.length === 0 ? (
          <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
            No findings match the current filters.
          </div>
        ) : (
          <div ref={parentRef} className="min-h-0 flex-1 overflow-auto pr-2">
            <div className="space-y-4">
              {findings.map((finding) => (
                <FindingCard
                  key={finding.id}
                  finding={finding}
                  projectId={projectIdByProduct.get(finding.productId ?? 0)}
                  projectVersionId={selectedProductId ? filterProjectVersionId : undefined}
                  onSelect={setSelected}
                />
              ))}
            </div>
          </div>
        )}
        {findingsQuery.isFetchingNextPage ? (
          <div className="rounded-2xl border border-night-500 bg-night-700 p-4 text-xs text-slate-300">
            Loading more findings...
          </div>
        ) : null}
      </div>

      <DetailPanel
        finding={selected ? { ...selected, projectVersionId } : undefined}
        aiResponse={aiResponse}
        pipelineId={selectedPipelinesQuery.data?.[0]?.id}
      />
    </div>
  );
}
