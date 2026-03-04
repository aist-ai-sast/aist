import { Link, useParams } from "react-router-dom";
import { useEffect, useState } from "react";
import FindingDetailTabs from "../components/FindingDetailTabs";
import AiVerdictBadge from "../components/AiVerdictBadge";
import CweTooltip from "../components/CweTooltip";
import SkeletonBlock from "../components/SkeletonBlock";
import {
  useAiFindingResponses,
  useAiResponse,
  useFinding,
  useFindingProjectVersion,
  useProjectMeta,
  useProjects,
} from "../lib/queries";
import { useExportFinding } from "../lib/mutations";
import { useToast } from "../components/ToastProvider";
import FindingStatusActions from "../components/FindingStatusActions";
import FindingSeverityControl from "../components/FindingSeverityControl";
import { formatProjectVersionText } from "../lib/projectVersion";
import { getRoute } from "../lib/routes";
import { formatDateForUI } from "../lib/dateDisplay";
import PageErrorState from "../components/PageErrorState";
import { findingStatusBadgeClass } from "../lib/badgeStyles";
import { getFindingStatusBadges } from "../lib/findingStatus";

export default function FindingDetailPage() {
  const params = useParams();
  const findingId = params.id ? Number(params.id) : undefined;
  const findingQuery = useFinding(findingId);
  const projectsQuery = useProjects();
  const [localFindingOverride, setLocalFindingOverride] = useState<Partial<NonNullable<typeof findingQuery.data>>>({});
  // Clear optimistic override when server data changes after a mutation.
  useEffect(() => {
    setLocalFindingOverride({});
  }, [findingQuery.dataUpdatedAt]);
  const finding = findingQuery.data ? { ...findingQuery.data, ...localFindingOverride } : undefined;
  const findingProjectVersionQuery = useFindingProjectVersion(findingId);
  const exportFinding = useExportFinding();
  const toast = useToast();

  const projects = projectsQuery.data ?? [];
  const resolvedProjectId = finding?.projectId ?? findingProjectVersionQuery.data?.projectId ?? undefined;
  const aistProject = projects.find((project) => project.id === resolvedProjectId);
  const projectName = aistProject?.name;
  const aiResponsesQuery = useAiFindingResponses(
    aistProject?.id,
    undefined,
    finding?.id ? [finding.id] : undefined,
  );
  const aiResponse = useAiResponse(aiResponsesQuery.data ?? new Map(), finding?.id);
  const metaQuery = useProjectMeta(aistProject?.id);
  const latestMetaVersion = metaQuery.data?.versions?.length
    ? metaQuery.data.versions[metaQuery.data.versions.length - 1]
    : undefined;
  const normalizedMetaVersion = latestMetaVersion?.label?.replace(/^\d+:\s*/, "");
  const resolvedProjectVersionType =
    finding?.projectVersionType ?? findingProjectVersionQuery.data?.versionType;
  const resolvedProjectVersion =
    finding?.projectVersion ?? findingProjectVersionQuery.data?.version ?? normalizedMetaVersion;
  // Only block on the primary finding error; secondary query failures degrade gracefully.
  const pageError = findingQuery.error ?? projectsQuery.error;
  const createdLabel = formatDateForUI(finding?.createdAt) ?? formatDateForUI(finding?.date);
  const findingsFilterLink = ({
    projectVersion,
    file,
  }: {
    projectVersion?: string;
    file?: string;
  }) => {
    const params = new URLSearchParams();
    if (resolvedProjectId) params.set("project", String(resolvedProjectId));
    if (projectVersion) params.set("project_version", projectVersion);
    if (file) params.set("file", file);
    const query = params.toString();
    return query ? `${getRoute("ui_findings_path")}?${query}` : getRoute("ui_findings_path");
  };

  if (findingQuery.isLoading) {
    return <SkeletonBlock />;
  }

  if (pageError) {
    return <PageErrorState error={pageError} fallbackTitle="Failed to load finding" />;
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
            <span className="inline-flex items-center gap-1">
              <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 3 4 7.5 12 12l8-4.5L12 3Z" />
                <path d="M4 7.5V16.5L12 21" />
                <path d="M20 7.5V16.5L12 21" />
              </svg>
              Project: {projectName ?? "Unknown"}
            </span>
            {finding.cwe ? (
              <span className="inline-flex items-center gap-1">
                <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 shrink-0" aria-hidden="true">
                  <path fill="currentColor" d="M12 2 4 5v6c0 5 3.4 9.7 8 11 4.6-1.3 8-6 8-11V5l-8-3Zm0 2.2 6 2.2V11c0 4.1-2.7 8-6 9.2-3.3-1.2-6-5.1-6-9.2V6.4l6-2.2Z" />
                </svg>
                <CweTooltip cwe={finding.cwe} />
              </span>
            ) : null}
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
                {formatProjectVersionText(resolvedProjectVersion, resolvedProjectVersionType)}
              </Link>
            ) : null}
            {finding.filePath ? (
              <Link
                to={findingsFilterLink({ file: finding.filePath })}
                className="aist-clickable-text inline-flex max-w-full items-center gap-1 truncate"
                title={finding.filePath}
              >
                <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 shrink-0" aria-hidden="true">
                  <path
                    fill="currentColor"
                    d="M6 2h8l4 4v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2Zm7 1.5V7h3.5L13 3.5ZM6 4v16h10V8h-4a1 1 0 0 1-1-1V4H6Z"
                  />
                </svg>
                <span className="truncate">File: {finding.filePath}</span>
              </Link>
            ) : null}
            {createdLabel ? (
              <span className="inline-flex items-center gap-1">
                <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
                  <path fill="currentColor" d="M7 2h2v2h6V2h2v2h3v18H4V4h3V2Zm11 8H6v10h12V10Zm0-4H6v2h12V6Z" />
                </svg>
                Created: {createdLabel}
              </span>
            ) : null}
            {finding.lastStatusUpdate ? (
              <span className="inline-flex items-center gap-1">
                <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
                  <path fill="currentColor" d="M12 4V1L8 5l4 4V6a6 6 0 1 1-6 6H4a8 8 0 1 0 8-8Z" />
                </svg>
                Updated: {formatDateForUI(finding.lastStatusUpdate)}
              </span>
            ) : null}
            {aiResponse?.pipelineId ? (
              <Link
                to={`${getRoute("ui_pipelines_path")}?pipeline=${aiResponse.pipelineId}`}
                className="aist-clickable-text inline-flex items-center gap-1"
              >
                <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
                  <path fill="currentColor" d="M4 6h2v2H4V6Zm0 4h2v2H4v-2Zm0 4h2v2H4v-2Zm4-8h12v2H8V6Zm0 4h12v2H8v-2Zm0 4h12v2H8v-2Z" />
                </svg>
                View pipeline
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
            <AiVerdictBadge verdict={aiResponse?.verdict} />
            {getFindingStatusBadges(finding).map((status) => (
              <span key={status} className={`rounded-full border px-3 py-1 text-xs ${findingStatusBadgeClass(status)}`}>
                {status}
              </span>
            ))}
          </div>
        </div>
        <div className="flex items-end gap-3">
          <FindingSeverityControl
            finding={finding}
            permissionProductId={aistProject?.productId}
            onChanged={(severity) => {
              setLocalFindingOverride({ severity });
            }}
          />
          <FindingStatusActions
            finding={finding}
            permissionProductId={aistProject?.productId}
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
              if (!finding?.id) {
                toast.push("Finding export is unavailable.", "error");
                return;
              }
              exportFinding.mutate(
                { findingId: finding.id },
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
            disabled={!finding?.id}
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
          finding={finding}
          permissionProductId={aistProject?.productId}
          aiResponse={aiResponse}
        />
      </section>
    </div>
  );
}
