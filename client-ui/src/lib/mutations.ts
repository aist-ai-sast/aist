import { useMutation, useQueryClient } from "@tanstack/react-query";

import { fetchBlob, fetchJson } from "./api";
import { getRoute } from "./routes";

export function useUpdateFindingStatus() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      active,
      clearCloseFlags,
    }: {
      id: number;
      active: boolean;
      clearCloseFlags?: boolean;
    }) => {
      const payload: Record<string, unknown> = { active };
      if (clearCloseFlags) {
        payload.is_mitigated = false;
        payload.false_p = false;
        payload.out_of_scope = false;
        payload.duplicate = false;
      }
      return fetchJson(getRoute("finding_detail_url", { id }), {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["findings"] });
      queryClient.invalidateQueries({ queryKey: ["finding"] });
    },
  });
}

export function useCloseFinding() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      reason,
    }: {
      id: number;
      reason: "mitigated" | "false_positive" | "out_of_scope" | "duplicate";
    }) => {
      const payload = {
        is_mitigated: reason === "mitigated",
        false_p: reason === "false_positive",
        out_of_scope: reason === "out_of_scope",
        duplicate: reason === "duplicate",
      };
      return fetchJson(getRoute("finding_close_url", { id }), {
        method: "POST",
        body: JSON.stringify(payload),
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["findings"] });
      queryClient.invalidateQueries({ queryKey: ["finding"] });
    },
  });
}

export function useAddFindingNote() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, entry }: { id: number; entry: string }) => {
      return fetchJson(getRoute("finding_notes_url", { finding_id: id }), {
        method: "POST",
        body: JSON.stringify({ entry, private: false }),
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["finding"] });
      queryClient.invalidateQueries({ queryKey: ["finding-notes"] });
    },
  });
}

function getFilenameFromDisposition(header: string | null) {
  if (!header) return "ai-results.xlsx";
  const match = header.match(/filename\\*=UTF-8''([^;]+)|filename=\"?([^\";]+)\"?/i);
  return decodeURIComponent(match?.[1] ?? match?.[2] ?? "ai-results.xlsx");
}

export function useExportAiResults() {
  return useMutation({
    mutationFn: async ({ pipelineId }: { pipelineId: string }) => {
      const resp = await fetchBlob(getRoute("pipeline_export_url", { pipeline_id: pipelineId }), {
        method: "POST",
      });
      const blob = await resp.blob();
      const filename = getFilenameFromDisposition(resp.headers.get("Content-Disposition"));
      const url = window.URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.URL.revokeObjectURL(url);
      return filename;
    },
  });
}

export function useExportFinding() {
  return useMutation({
    mutationFn: async ({ findingId }: { findingId: number }) => {
      const resp = await fetchBlob(getRoute("finding_export_url", { finding_id: findingId }), {
        method: "POST",
      });
      const blob = await resp.blob();
      const filename = getFilenameFromDisposition(resp.headers.get("Content-Disposition"));
      const url = window.URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.URL.revokeObjectURL(url);
      return filename;
    },
  });
}
