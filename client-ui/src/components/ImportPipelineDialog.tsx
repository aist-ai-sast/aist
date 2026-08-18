import { useEffect, useState } from "react";
import { useDropzone } from "react-dropzone";

import { toUserMessage } from "../lib/api";
import { severityBadgeClass } from "../lib/badgeStyles";
import { dastBindingLabel, targetRequiresSourceRepository } from "../lib/dast";
import { useImportPipeline, useValidateImportPipeline } from "../lib/mutations";
import type { ImportPipelinePreview } from "../lib/mutations";
import { formatCompactTokens, formatCount } from "../lib/dastRun";
import { usePipelineStatus, useProjectDastBindings, useProjects } from "../lib/queries";
import type { Severity } from "../types";
import Modal from "./Modal";
import SelectField from "./SelectField";
import { useToast } from "./ToastProvider";

type DialogState = "upload" | "invalid" | "valid" | "progress";

const SCAN_TYPE = "DAST Autonomous Scan";
const TERMINAL_STATUSES = new Set(["FINISHED", "FINISHED_WITH_WARNINGS"]);

/** Shared with the "Import pipeline launch" trigger button so the same action reads as one visual language. */
export function ImportUploadIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 15V4M12 4 8 8M12 4l4 4" />
      <path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" />
    </svg>
  );
}

/**
 * What the run covered, shown before the operator commits the import. Reads the same counters
 * the pipeline list will show afterwards, so the two cannot disagree.
 */
function ImportRunCoverage({ run }: { run: NonNullable<ImportPipelinePreview["dast_run"]> }) {
  const unit = run.coverage_unit ? `${run.coverage_unit}s` : "targets";
  const coverage =
    run.analysed !== null && run.reachable !== null
      ? `${formatCount(run.analysed)} / ${formatCount(run.reachable)} ${unit}`
      : run.analysed !== null
        ? `${formatCount(run.analysed)} ${unit}`
        : null;
  const tokens = formatCompactTokens(run.total_tokens);
  const calls = formatCount(run.model_calls);
  if (!coverage && !tokens) return null;

  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      {coverage ? (
        <span className="rounded-full border border-night-500 bg-night-800 px-3 py-1 text-slate-200">
          {coverage} analysed
        </span>
      ) : null}
      {tokens ? (
        <span className="rounded-full border border-night-500 bg-night-800 px-3 py-1 text-slate-200">
          {tokens} tokens{calls ? ` · ${calls} calls` : ""}
        </span>
      ) : null}
      {run.beyond_plan !== null && run.beyond_plan > 0 ? (
        <span className="rounded-full border border-amber-400/35 bg-amber-400/10 px-3 py-1 text-amber-400">
          {formatCount(run.beyond_plan)} beyond plan
        </span>
      ) : null}
    </div>
  );
}

export default function ImportPipelineDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [state, setState] = useState<DialogState>("upload");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<ImportPipelinePreview | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [projectId, setProjectId] = useState<string>("");
  const [bindingId, setBindingId] = useState<string>("");
  const [pipelineId, setPipelineId] = useState<string | null>(null);

  const { data: projects = [] } = useProjects();
  const { data: projectBindings = [], isLoading: bindingsLoading } = useProjectDastBindings(
    projectId ? Number(projectId) : undefined,
  );
  const enabledBindings = projectBindings.filter((binding) => binding.enabled);
  const selectedBinding = enabledBindings.find((binding) => String(binding.id) === bindingId) ?? null;
  const requiresSourceRepository = selectedBinding ? targetRequiresSourceRepository(selectedBinding.target) : true;
  const validateMutation = useValidateImportPipeline();
  const importMutation = useImportPipeline();
  const statusQuery = usePipelineStatus(state === "progress" ? pipelineId : null);
  const toast = useToast();

  const status = statusQuery.data?.status;
  const isTerminal = Boolean(status && TERMINAL_STATUSES.has(status) && !statusQuery.data?.run_task_id);

  useEffect(() => {
    if (state !== "progress" || !isTerminal) return;
    toast.push(
      status === "FINISHED_WITH_WARNINGS" ? "Report imported with warnings." : "Report imported.",
      status === "FINISHED_WITH_WARNINGS" ? "info" : "success",
    );
    reset();
    onClose();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isTerminal]);

  function reset() {
    setState("upload");
    setFile(null);
    setPreview(null);
    setProblem(null);
    setProjectId("");
    setBindingId("");
    setPipelineId(null);
  }

  function handleProjectChange(value: string) {
    setProjectId(value);
    setBindingId("");
    setFile(null);
    setPreview(null);
    setProblem(null);
    setState("upload");
  }

  function handleBindingChange(value: string) {
    setBindingId(value);
    setFile(null);
    setPreview(null);
    setProblem(null);
    setState("upload");
  }

  function handleClose() {
    if (state === "progress") return; // import is in flight; let it finish
    reset();
    onClose();
  }

  async function handleFile(selected: File) {
    setFile(selected);
    if (!projectId || !bindingId) {
      setProblem("Select a target project and DAST target before choosing a file.");
      setState("invalid");
      return;
    }
    try {
      const result = await validateMutation.mutateAsync({
        file: selected,
        projectId: Number(projectId),
        bindingId: Number(bindingId),
        scanType: SCAN_TYPE,
      });
      setPreview(result);
      setProblem(null);
      setState("valid");
    } catch (err) {
      setPreview(null);
      setProblem(toUserMessage(err));
      setState("invalid");
    }
  }

  async function handleImport() {
    if (!file || !projectId || !bindingId) return;
    if (requiresSourceRepository && !preview?.actual_source_commit) return;
    try {
      const result = await importMutation.mutateAsync({
        file,
        projectId: Number(projectId),
        bindingId: Number(bindingId),
        scanType: SCAN_TYPE,
      });
      setPipelineId(result.pipeline_id);
      setState("progress");
    } catch (err) {
      setProblem(toUserMessage(err));
      setState("invalid");
    }
  }

  const isUploadDisabled = !projectId || !bindingId || validateMutation.isPending;
  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop: (acceptedFiles) => {
      const dropped = acceptedFiles[0];
      if (dropped) void handleFile(dropped);
    },
    disabled: isUploadDisabled,
    multiple: false,
    noKeyboard: false,
  });

  return (
    <Modal open={open} onClose={handleClose} title="Import report">
      <div className="flex flex-col gap-4">
        <h2 className="text-base font-semibold text-white">Import report</h2>

        {state === "upload" ? (
          <>
            <SelectField
              label="Target project"
              value={projectId}
              onChange={handleProjectChange}
              options={projects.map((project) => ({ value: String(project.id), label: project.name }))}
              placeholder="Select the project this report applies to"
            />
            <SelectField
              label="DAST target"
              value={bindingId}
              onChange={handleBindingChange}
              options={enabledBindings.map((binding) => ({
                value: String(binding.id),
                label: dastBindingLabel(binding),
              }))}
              placeholder={bindingsLoading ? "Loading DAST targets…" : "Select the bound DAST target"}
              disabled={!projectId || bindingsLoading || enabledBindings.length === 0}
            />
            {projectId && !bindingsLoading && enabledBindings.length === 0 ? (
              <p className="text-xs text-amber-300">
                This project has no enabled DAST target binding. Configure one in Integrations before importing.
              </p>
            ) : null}
            <div
              {...getRootProps({ role: "button", "aria-disabled": isUploadDisabled })}
              className={[
                "flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed px-6 py-10 text-sm transition outline-none",
                isUploadDisabled
                  ? "cursor-not-allowed border-night-500 bg-night-800 text-slate-500"
                  : isDragActive
                    ? "cursor-pointer border-brand-600 bg-brand-500/10 text-slate-100"
                    : "cursor-pointer border-night-500 bg-night-800 text-slate-400 hover:border-brand-600 hover:text-slate-200",
              ].join(" ")}
            >
              <input {...getInputProps()} />
              <ImportUploadIcon className="h-5 w-5" />
              <span>
                {validateMutation.isPending
                  ? "Validating…"
                  : isDragActive
                    ? "Drop to upload"
                    : "Drop a report file here, or click to browse"}
              </span>
            </div>
          </>
        ) : null}

        {(state === "valid" || state === "invalid") && file ? (
          <div className="text-xs text-slate-400">
            File: <span className="text-slate-200">{file.name}</span>
          </div>
        ) : null}

        {state === "invalid" ? (
          <div className="rounded-xl border border-danger-500/40 bg-danger-500/10 p-3 text-sm text-danger-100">
            <p className="font-medium">This report can&apos;t be imported:</p>
            <p className="mt-1">{problem}</p>
          </div>
        ) : null}

        {state === "valid" && preview ? (
          <div className="flex flex-col gap-4">
            <div className="grid grid-cols-2 gap-3 text-xs">
              <div>
                <span className="text-slate-400">Report</span>
                <p className="text-slate-100">
                  {preview.name || "—"} {preview.version || ""}
                </p>
              </div>
              <div>
                <span className="text-slate-400">Findings</span>
                <p className="text-slate-100">{preview.findings_count}</p>
              </div>
            </div>

            {preview.dast_run ? <ImportRunCoverage run={preview.dast_run} /> : null}

            <div className="flex flex-wrap gap-2">
              {(Object.entries(preview.severity_breakdown) as [Severity, number][]).map(([severity, count]) => (
                <span
                  key={severity}
                  className={`rounded-full border px-2 py-0.5 text-[11px] ${severityBadgeClass(severity)}`}
                >
                  {severity}: {count}
                </span>
              ))}
            </div>

            {requiresSourceRepository ? (
              <div className="flex flex-col gap-1 text-xs text-slate-400">
                <span>Actual source commit</span>
                <code className="break-all rounded-lg border border-night-500 bg-night-800 px-3 py-2 text-sm text-slate-100">
                  {preview.actual_source_commit || "—"}
                </code>
                <span className="text-[11px] text-slate-500">
                  Verified from the signed DAST result for the selected binding; it cannot be overridden.
                </span>
              </div>
            ) : (
              <div className="flex flex-col gap-1 text-xs text-slate-400">
                <span className="text-[11px] text-slate-500">
                  This target has no source repository requirement; the pipeline will not be linked to a project version.
                </span>
              </div>
            )}

            <div className="flex justify-end gap-2">
              <button
                type="button"
                className="rounded-xl border border-night-500 px-4 py-2 text-sm text-slate-300 hover:bg-night-600"
                onClick={handleClose}
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={(requiresSourceRepository && !preview.actual_source_commit) || importMutation.isPending}
                className="rounded-xl bg-brand-600 px-4 py-2 text-sm font-medium text-night-900 transition disabled:cursor-not-allowed disabled:opacity-50 hover:bg-brand-500"
                onClick={handleImport}
              >
                {importMutation.isPending ? "Creating pipeline…" : "Create pipeline"}
              </button>
            </div>
          </div>
        ) : null}

        {state === "invalid" ? (
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="rounded-xl border border-night-500 px-4 py-2 text-sm text-slate-300 hover:bg-night-600"
              onClick={handleClose}
            >
              Close
            </button>
          </div>
        ) : null}

        {state === "progress" ? (
          <div className="flex flex-col gap-3">
            <div className="h-2 w-full overflow-hidden rounded-full bg-night-800">
              <div className="h-full w-1/3 animate-pulse rounded-full bg-brand-600 motion-reduce:animate-none" />
            </div>
            <p className="text-sm text-slate-300">
              Importing report… ({status ?? "starting"})
            </p>
          </div>
        ) : null}
      </div>
    </Modal>
  );
}
