import { useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";

import FilterPanel from "../components/FilterPanel";
import FindingCard from "../components/FindingCard";
import DetailPanel from "../components/DetailPanel";
import type { Finding } from "../types";
import { useAiResponse, useFindingsWithFilters, usePipelines, useProjectMeta, useProjects } from "../lib/queries";
import { useToast } from "../components/ToastProvider";

export default function FindingsPage() {
  const [selectedProductId, setSelectedProductId] = useState<number | undefined>();
  const [selectedSeverity, setSelectedSeverity] = useState<string>("All severities");
  const [selectedStatus, setSelectedStatus] = useState<string>("All");
  const [selectedRisk, setSelectedRisk] = useState<string[]>([]);
  const [selectedAiVerdict, setSelectedAiVerdict] = useState<string>("All");
  const [selectedSort, setSelectedSort] = useState<string>("severity");
  const toast = useToast();
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
    status: selectedStatus === "Enabled" ? "enabled" : selectedStatus === "Disabled" ? "disabled" : undefined,
    riskStates: selectedRisk.length ? (selectedRisk as any) : undefined,
    limit: pageSize,
    ordering,
  });

  const projects = projectsQuery.data ?? [];
  const aistProjectForFilters = projects.find((project) => project.productId === selectedProductId);
  const pipelinesQuery = usePipelines(aistProjectForFilters?.id);

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

  const [selected, setSelected] = useState<Finding | undefined>(findings[0]);
  const parentRef = useRef<HTMLDivElement | null>(null);

  const aistProjectForSelected = projects.find((project) => project.productId === selected?.productId);
  const selectedPipelinesQuery = usePipelines(aistProjectForSelected?.id);
  const aiResponse = useAiResponse(selectedPipelinesQuery.data ?? [], selected?.id);

  const metaQuery = useProjectMeta(aistProjectForSelected?.id);
  const projectVersionId = metaQuery.data?.versions?.length
    ? Number(metaQuery.data.versions[metaQuery.data.versions.length - 1].id)
    : undefined;

  const rowVirtualizer = useVirtualizer({
    count: findings.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 230,
    overscan: 6,
  });

  const virtualItems = rowVirtualizer.getVirtualItems();

  useEffect(() => {
    const [lastItem] = [...virtualItems].reverse();
    if (!lastItem) return;
    if (lastItem.index >= findings.length - 3 && findingsQuery.hasNextPage && !findingsQuery.isFetchingNextPage) {
      findingsQuery.fetchNextPage();
    }
  }, [
    virtualItems,
    findings.length,
    findingsQuery.hasNextPage,
    findingsQuery.isFetchingNextPage,
    findingsQuery.fetchNextPage,
  ]);

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
    <div className="grid gap-6 lg:grid-cols-[280px_1fr_360px]">
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
        selectedAiVerdict={selectedAiVerdict}
        onAiVerdictChange={setSelectedAiVerdict}
        aiVerdictDisabled={!selectedProductId}
      />

      <div className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs uppercase tracking-[0.2em] text-slate-400">
          <span>Findings</span>
          <div className="flex items-center gap-2">
            <button
              className="rounded-xl border border-night-500 bg-night-700 px-3 py-2 text-xs text-slate-200"
              onClick={exportCurrentView}
            >
              Export current view
            </button>
            <select
              className="rounded-xl border border-night-500 bg-night-700 px-3 py-2 text-xs text-slate-200"
              value={selectedSort}
              onChange={(event) => setSelectedSort(event.target.value)}
            >
              <option value="severity">Sort: Severity</option>
              <option value="date">Sort: Date</option>
              <option value="title">Sort: Title</option>
            </select>
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
          <div
            ref={parentRef}
            className="h-[calc(100vh-200px)] overflow-auto pr-2"
          >
            <div
              style={{
                height: `${rowVirtualizer.getTotalSize()}px`,
                width: "100%",
                position: "relative",
              }}
            >
              {rowVirtualizer.getVirtualItems().map((virtualRow) => {
                const finding = findings[virtualRow.index];
                return (
                  <div
                    key={finding.id}
                    style={{
                      position: "absolute",
                      top: 0,
                      left: 0,
                      width: "100%",
                      transform: `translateY(${virtualRow.start}px)`,
                    }}
                  >
                    <FindingCard finding={finding} onSelect={setSelected} />
                  </div>
                );
              })}
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
