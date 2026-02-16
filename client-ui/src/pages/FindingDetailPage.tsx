import { Link, useParams } from "react-router-dom";
import { useState } from "react";
import FindingDetailTabs from "../components/FindingDetailTabs";
import {
  useAiResponse,
  useEngagementProduct,
  useFinding,
  useFindingProjectVersion,
  usePipelines,
  useProjectMeta,
  useProjects,
  useTestEngagement,
} from "../lib/queries";
import { useExportAiResults } from "../lib/mutations";
import { useToast } from "../components/ToastProvider";
import FindingStatusActions from "../components/FindingStatusActions";
import { getRoute } from "../lib/routes";

export default function FindingDetailPage() {
  const params = useParams();
  const findingId = params.id ? Number(params.id) : undefined;
  const findingQuery = useFinding(findingId);
  const projectsQuery = useProjects();
  const [localFindingOverride, setLocalFindingOverride] = useState<Partial<NonNullable<typeof findingQuery.data>>>({});
  const finding = findingQuery.data ? { ...findingQuery.data, ...localFindingOverride } : undefined;
  const findingProjectVersionQuery = useFindingProjectVersion(findingId);
  const exportAi = useExportAiResults();
  const toast = useToast();

  const projects = projectsQuery.data ?? [];
  const testEngagementQuery = useTestEngagement(finding?.testId ?? null);
  const engagementProductQuery = useEngagementProduct(testEngagementQuery.data ?? null);
  const resolvedProductId = finding?.productId ?? engagementProductQuery.data ?? undefined;
  const aistProject = projects.find((project) => project.productId === resolvedProductId);
  const productName = projects.find((project) => project.productId === resolvedProductId)?.name;
  const pipelinesQuery = usePipelines(aistProject?.id);
  const aiResponse = useAiResponse(pipelinesQuery.data ?? [], finding?.id);
  const metaQuery = useProjectMeta(aistProject?.id);
  const projectVersionId = metaQuery.data?.versions?.length
    ? Number(metaQuery.data.versions[metaQuery.data.versions.length - 1].id)
    : undefined;
  const latestMetaVersion = metaQuery.data?.versions?.length
    ? metaQuery.data.versions[metaQuery.data.versions.length - 1]
    : undefined;
  const normalizedMetaVersion = latestMetaVersion?.label?.replace(/^\d+:\s*/, "");
  const resolvedProjectVersion =
    finding?.projectVersion ?? findingProjectVersionQuery.data ?? normalizedMetaVersion;
  const findingsFilterLink = ({
    projectVersion,
    file,
  }: {
    projectVersion?: string;
    file?: string;
  }) => {
    const params = new URLSearchParams();
    if (resolvedProductId) params.set("product", String(resolvedProductId));
    if (projectVersion) params.set("project_version", projectVersion);
    if (file) params.set("file", file);
    const query = params.toString();
    return query ? `${getRoute("ui_findings_path")}?${query}` : getRoute("ui_findings_path");
  };

  if (findingQuery.isLoading) {
    return (
      <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
        Loading finding...
      </div>
    );
  }

  if (!finding) {
    return (
      <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
        Finding not found. <Link to={getRoute("ui_findings_path")}>Back to Findings</Link>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="text-xs uppercase tracking-[0.2em] text-slate-400">
            Finding Detail
          </div>
          <h1 className="mt-2 text-2xl font-semibold line-clamp-2" title={finding.title}>
            {finding.title}
          </h1>
          <div className="mt-2 flex flex-wrap gap-3 text-xs text-slate-400">
            <span>Product: {productName ?? finding.product}</span>
            {finding.cwe ? <span>CWE: {finding.cwe}</span> : null}
            {resolvedProjectVersion ? (
              <Link
                to={findingsFilterLink({ projectVersion: resolvedProjectVersion })}
                className="aist-clickable-text inline-flex items-center gap-1"
              >
                <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
                  <path
                    fill="currentColor"
                    d="M7 6a3 3 0 1 1 2.83 4H9v4h1a3 3 0 1 1 0 2H9a2 2 0 0 1-2-2v-4a3 3 0 0 1 0-4Z"
                  />
                </svg>
                Version: {resolvedProjectVersion}
              </Link>
            ) : null}
            {finding.filePath ? (
              <Link
                to={findingsFilterLink({ file: finding.filePath })}
                className="aist-clickable-text max-w-full truncate"
                title={finding.filePath}
              >
                File: {finding.filePath}
              </Link>
            ) : null}
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            <span
              className={[
                "rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-wide",
                finding.severity === "Critical"
                  ? "border-danger-500/50 text-danger-500 bg-danger-500/10"
                  : finding.severity === "High"
                    ? "border-danger-500/30 text-danger-500/80 bg-danger-500/10"
                    : finding.severity === "Medium"
                      ? "border-amber-400/40 text-amber-400 bg-amber-400/10"
                      : finding.severity === "Low"
                        ? "border-slate-500/40 text-slate-300 bg-slate-500/10"
                        : "border-slate-500/40 text-slate-300 bg-slate-500/10",
              ].join(" ")}
            >
              {finding.severity}
            </span>
            {finding.isMitigated ? (
              <span className="rounded-full border border-emerald-400/40 bg-emerald-400/10 px-3 py-1 text-xs text-emerald-300">
                Mitigated
              </span>
            ) : null}
            {finding.riskAccepted ? (
              <span className="rounded-full border border-amber-400/40 bg-amber-400/10 px-3 py-1 text-xs text-amber-300">
                Risk Accepted
              </span>
            ) : null}
            {finding.falsePositive ? (
              <span className="rounded-full border border-purple-400/40 bg-purple-400/10 px-3 py-1 text-xs text-purple-300">
                False Positive
              </span>
            ) : null}
            {finding.outOfScope ? (
              <span className="rounded-full border border-slate-400/40 bg-slate-400/10 px-3 py-1 text-xs text-slate-300">
                Out of Scope
              </span>
            ) : null}
            {finding.duplicate ? (
              <span className="rounded-full border border-slate-400/40 bg-slate-400/10 px-3 py-1 text-xs text-slate-300">
                Duplicate
              </span>
            ) : null}
            {!finding.isMitigated && !finding.riskAccepted && !finding.falsePositive && !finding.outOfScope && !finding.duplicate ? (
              <span className="rounded-full border border-night-500 bg-night-900 px-3 py-1 text-xs text-slate-200">
                {finding.active ? "Active" : "Non-Active"}
              </span>
            ) : null}
          </div>
        </div>
        <div className="flex items-end gap-3">
          <FindingStatusActions
            finding={finding}
            onApplied={(reason) => {
              setLocalFindingOverride({
                active: false,
                isMitigated: reason === "mitigated",
                falsePositive: reason === "false_positive",
                outOfScope: reason === "out_of_scope",
                duplicate: reason === "duplicate",
              });
            }}
            onReopened={() => {
              setLocalFindingOverride({
                active: true,
                isMitigated: false,
                falsePositive: false,
                outOfScope: false,
                duplicate: false,
              });
            }}
          />
          <button
            className="aist-icon-button h-10 disabled:opacity-50"
            onClick={() => {
              if (!aiResponse?.pipelineId) {
                toast.push("AI results are not available for export.", "error");
                return;
              }
              exportAi.mutate(
                { pipelineId: aiResponse.pipelineId },
                {
                  onSuccess: () => {
                    toast.push("Export started.", "success");
                  },
                  onError: (error) => {
                    const message = error instanceof Error ? error.message : String(error);
                    toast.push(`Export failed: ${message}`, "error");
                  },
                },
              );
            }}
            disabled={!aiResponse?.pipelineId}
          >
            <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
              <path
                fill="currentColor"
                d="M12 3l4 4h-3v6h-2V7H8l4-4Zm-7 12h14v4a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-4Zm3 2v2h8v-2H8Z"
              />
            </svg>
            Export
          </button>
        </div>
      </div>

      <section className="rounded-2xl border border-night-500 bg-night-700 p-5 shadow-panel">
        <FindingDetailTabs
          finding={{ ...finding, projectVersionId }}
          aiResponse={aiResponse}
        />
      </section>
    </div>
  );
}
