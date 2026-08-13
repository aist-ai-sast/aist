import { useMutation, useQueryClient } from "@tanstack/react-query";

import { fetchBlob, fetchJson, postFormData } from "./api";
import { getRoute } from "./routes";

export type FindingCloseReason = "mitigated" | "false_positive" | "out_of_scope" | "duplicate";
export type FindingSeverity = "Critical" | "High" | "Medium" | "Low" | "Info";

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
      reason: FindingCloseReason;
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

export function useRiskApproveFinding() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      justification,
      acceptedBy,
      expirationDate,
      reactivateExpired,
    }: {
      id: number;
      justification: string;
      acceptedBy?: string;
      expirationDate?: string;
      reactivateExpired?: boolean;
    }) => {
      const payload: Record<string, unknown> = {
        justification,
      };
      if (acceptedBy && acceptedBy.trim()) {
        payload.accepted_by = acceptedBy.trim();
      }
      if (expirationDate) {
        payload.expiration_date = expirationDate;
      }
      if (reactivateExpired !== undefined) {
        payload.reactivate_expired = reactivateExpired;
      }
      return fetchJson(getRoute("finding_risk_approval_url", { finding_id: id }), {
        method: "POST",
        body: JSON.stringify(payload),
      });
    },
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ["findings"] });
      queryClient.invalidateQueries({ queryKey: ["finding"] });
      queryClient.invalidateQueries({ queryKey: ["finding-timeline", variables.id] });
      queryClient.invalidateQueries({ queryKey: ["finding-notes", variables.id] });
      queryClient.invalidateQueries({ queryKey: ["risk-approval-status", variables.id] });
    },
  });
}

export function useRevokeRiskApproval() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id }: { id: number }) => {
      return fetchJson(getRoute("finding_risk_approval_url", { finding_id: id }), {
        method: "DELETE",
      });
    },
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ["findings"] });
      queryClient.invalidateQueries({ queryKey: ["finding"] });
      queryClient.invalidateQueries({ queryKey: ["finding-timeline", variables.id] });
      queryClient.invalidateQueries({ queryKey: ["finding-notes", variables.id] });
      queryClient.invalidateQueries({ queryKey: ["risk-approval-status", variables.id] });
    },
  });
}

export function useMarkFindingDuplicate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, originalFindingId }: { id: number; originalFindingId: number }) =>
      fetchJson(getRoute("finding_mark_duplicate_url", { finding_id: id }), {
        method: "POST",
        body: JSON.stringify({ original_finding_id: originalFindingId }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["findings"] });
      queryClient.invalidateQueries({ queryKey: ["finding"] });
    },
  });
}

export function useUpdateFindingSeverity() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      severity,
    }: {
      id: number;
      severity: FindingSeverity;
    }) => fetchJson(getRoute("finding_detail_url", { id }), {
      method: "PATCH",
      body: JSON.stringify({ severity }),
    }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["findings"] });
      queryClient.invalidateQueries({ queryKey: ["findings-page"] });
      queryClient.invalidateQueries({ queryKey: ["finding"] });
      queryClient.invalidateQueries({ queryKey: ["finding-timeline"] });
    },
  });
}

export function useBulkFindingStatus() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      findingIds,
      action,
      reason,
      closeReason,
    }: {
      findingIds: number[];
      action: "close" | "reopen" | "risk_accept";
      reason: string;
      closeReason?: FindingCloseReason;
    }) => {
      const payload: Record<string, unknown> = {
        finding_ids: findingIds,
        action,
        reason,
      };
      if (action === "close") {
        payload.close_reason = closeReason ?? "mitigated";
      }
      return fetchJson<{ updated_count: number; updated_ids: number[] }>(getRoute("finding_bulk_status_url"), {
        method: "POST",
        body: JSON.stringify(payload),
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["findings"] });
      queryClient.invalidateQueries({ queryKey: ["findings-page"] });
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
      const blob = resp.data;
      const filename = getFilenameFromDisposition(resp.headers["content-disposition"] ?? null);
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

// ---------------------------------------------------------------------------
// WorkItemProvider mutations
// ---------------------------------------------------------------------------

export type WorkItemProviderPayload = {
  provider_type: string;
  name: string;
  base_url?: string;
  api_token?: string;
  provider_config?: Record<string, unknown>;
  sync_enabled?: boolean;
  is_active?: boolean;
  vpn_integration?: number | null;
};

export function useCreateWorkItemProvider(orgId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: WorkItemProviderPayload) =>
      fetchJson(getRoute("work_item_providers_url", { org_id: orgId }), {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["work-item-providers", orgId] });
    },
  });
}

export function useUpdateWorkItemProvider(orgId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ providerId, payload }: { providerId: number; payload: Partial<WorkItemProviderPayload> }) =>
      fetchJson(getRoute("work_item_provider_detail_url", { provider_id: providerId }), {
        method: "PATCH",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["work-item-providers", orgId] });
    },
  });
}

export function useDeleteWorkItemProvider(orgId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (providerId: number) =>
      fetchJson(getRoute("work_item_provider_detail_url", { provider_id: providerId }), { method: "DELETE" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["work-item-providers", orgId] });
    },
  });
}

export function useValidateWorkItemProvider() {
  return useMutation({
    mutationFn: (providerId: number) =>
      fetchJson<{ task_id: string }>(
        getRoute("work_item_provider_validate_url", { provider_id: providerId }),
        { method: "POST" },
      ),
  });
}

export function useSyncWorkItemProvider() {
  return useMutation({
    mutationFn: (providerId: number) =>
      fetchJson<{ queued: boolean }>(
        getRoute("work_item_provider_sync_url", { provider_id: providerId }),
        { method: "POST" },
      ),
  });
}

// ---------------------------------------------------------------------------
// Per-project integration override mutations
// ---------------------------------------------------------------------------

export function useSetProjectIntegrationOverride(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      integrationType,
      orgIntegrationId,
      configOverride,
      isDisabled,
    }: {
      integrationType: string;
      orgIntegrationId?: number | null;
      configOverride?: Record<string, unknown>;
      isDisabled?: boolean;
    }) =>
      fetchJson(
        getRoute("project_integration_override_detail_url", {
          project_id: projectId,
          integration_type: integrationType,
        }),
        {
          method: "PUT",
          body: JSON.stringify({
            org_integration: orgIntegrationId ?? null,
            config_override: configOverride ?? {},
            is_disabled: isDisabled ?? false,
          }),
        },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["project-integration-overrides", projectId] });
    },
  });
}

export function useDeleteProjectIntegrationOverride(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (integrationType: string) =>
      fetchJson(
        getRoute("project_integration_override_detail_url", {
          project_id: projectId,
          integration_type: integrationType,
        }),
        { method: "DELETE" },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["project-integration-overrides", projectId] });
    },
  });
}

export type DastBindingPayload = {
  target_id: number;
  capability_revision: string;
  schema_digest: string;
  source_repo_key: string;
  enabled: boolean;
  parameter_snapshot: Record<string, unknown>;
};

export function useUpsertDastProjectBinding(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: DastBindingPayload) =>
      fetchJson(getRoute("project_dast_bindings_url", { project_id: projectId }), {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["dast-bindings", projectId] });
    },
  });
}

export function useDeleteDastProjectBinding(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (bindingId: number) =>
      fetchJson(getRoute("dast_binding_detail_url", { binding_id: bindingId }), { method: "DELETE" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["dast-bindings", projectId] });
    },
  });
}

// DAST launch configs are created in the AIST admin launch dashboard, alongside SAST ones.
// client-ui deliberately has no create mutation: it had no list, edit, start or schedule
// surface to go with it, so a config created here was unreachable afterwards.

// ---------------------------------------------------------------------------
// Work item link mutations
// ---------------------------------------------------------------------------

export function useCreateWorkItem(findingId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: {
      external_url: string;
      external_key?: string;
      title?: string;
      provider?: number | null;
    }) => fetchJson(
      getRoute("finding_work_items_url", { finding_id: findingId }),
      { method: "POST", body: JSON.stringify(payload) },
    ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["work-items", findingId] });
      queryClient.invalidateQueries({ queryKey: ["findings-page"] });
      queryClient.invalidateQueries({ queryKey: ["finding", findingId] });
    },
  });
}

export function useDeleteWorkItem(findingId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (linkId: number) => fetchJson(
      getRoute("work_item_link_detail_url", { finding_id: findingId, link_id: linkId }),
      { method: "DELETE" },
    ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["work-items", findingId] });
      queryClient.invalidateQueries({ queryKey: ["findings-page"] });
      queryClient.invalidateQueries({ queryKey: ["finding", findingId] });
    },
  });
}

export function useUpdateWorkItem(findingId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ linkId, status_category }: { linkId: number; status_category: string }) =>
      fetchJson(
        getRoute("work_item_link_detail_url", { finding_id: findingId, link_id: linkId }),
        { method: "PATCH", body: JSON.stringify({ status_category }) },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["work-items", findingId] });
      queryClient.invalidateQueries({ queryKey: ["findings-page"] });
      queryClient.invalidateQueries({ queryKey: ["finding", findingId] });
    },
  });
}

export type VpnSecretPayload = {
  ovpn_content?: string;
  ca_cert?: string;
  client_cert?: string;
  client_key?: string;
  tls_auth_key?: string;
  vpn_username?: string;
  vpn_password?: string;
};

export type OrgIntegrationPayload = {
  integration_type: string;
  name: string;
  config?: Record<string, unknown>;
  secret?: string;
  vpn_secret?: VpnSecretPayload;
  vpn_integration?: number | null;
  is_active?: boolean;
};

export type DastOnboardingBundle = {
  bundle_version: 1;
  gateway_url: string;
  ca_bundle: string;
  contract_major: 2;
  integrator_public_id: string;
  server_fingerprint: string;
  token: string;
};

export type DastOnboardingPayload = {
  name?: string;
  vpn_integration_id?: number | null;
  bundle: DastOnboardingBundle;
};

/**
 * An update may omit the bundle. The stored integrator token is never readable, so rebuilding a
 * bundle from what the UI holds would send an empty token; omitting it keeps the stored connection
 * and token untouched and edits only the fields that are present.
 */
export type DastOnboardingUpdatePayload = Omit<DastOnboardingPayload, "bundle"> & {
  bundle?: DastOnboardingBundle;
};

export function useImportDastIntegration(orgId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: DastOnboardingPayload) =>
      fetchJson(getRoute("dast_integration_import_url", { org_id: orgId }), {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["org-integrations", orgId] }),
  });
}

export function useUpdateDastIntegrationOnboarding(orgId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ integrationId, payload }: { integrationId: number; payload: DastOnboardingUpdatePayload }) =>
      fetchJson(getRoute("dast_integration_onboarding_url", { integration_id: integrationId }), {
        method: "PATCH",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["org-integrations", orgId] }),
  });
}

export function useSyncDastCapabilities(orgId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (integrationId: number) =>
      fetchJson(getRoute("dast_integration_sync_capabilities_url", { integration_id: integrationId }), {
        method: "POST",
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["org-integrations", orgId] });
      queryClient.invalidateQueries({ queryKey: ["dast-targets", orgId] });
    },
  });
}

export function useRotateDastIntegrationToken(orgId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ integrationId, token }: { integrationId: number; token: string }) =>
      fetchJson(getRoute("dast_integration_rotate_token_url", { integration_id: integrationId }), {
        method: "POST",
        body: JSON.stringify({ token }),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["org-integrations", orgId] }),
  });
}

export function useCreateOrgIntegration(orgId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: OrgIntegrationPayload) =>
      fetchJson(getRoute("org_integrations_url", { org_id: orgId }), {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["org-integrations", orgId] });
    },
  });
}

export function useUpdateOrgIntegration(orgId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ integrationId, payload }: { integrationId: number; payload: Partial<OrgIntegrationPayload> }) =>
      fetchJson(getRoute("org_integration_detail_url", { integration_id: integrationId }), {
        method: "PATCH",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["org-integrations", orgId] });
    },
  });
}

export function useDeleteOrgIntegration(orgId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (integrationId: number) =>
      fetchJson(getRoute("org_integration_detail_url", { integration_id: integrationId }), { method: "DELETE" }),
    onSuccess: () => invalidateDastLifecycleQueries(queryClient, orgId),
  });
}

function invalidateDastLifecycleQueries(queryClient: ReturnType<typeof useQueryClient>, orgId: number) {
  queryClient.invalidateQueries({ queryKey: ["org-integrations", orgId] });
  queryClient.invalidateQueries({ queryKey: ["dast-targets", orgId] });
  queryClient.invalidateQueries({ queryKey: ["dast-bindings"] });
  queryClient.invalidateQueries({ queryKey: ["launch-configs"] });
  queryClient.invalidateQueries({ queryKey: ["launch-schedules"] });
  queryClient.invalidateQueries({ queryKey: ["launch-requests"] });
}

export function useDisableDastIntegration(orgId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (integrationId: number) =>
      fetchJson(getRoute("dast_integration_disable_url", { integration_id: integrationId }), { method: "POST" }),
    onSuccess: () => invalidateDastLifecycleQueries(queryClient, orgId),
  });
}

export type ImportPipelinePreview = {
  findings_count: number;
  severity_breakdown: Record<string, number>;
  name: string | null;
  version: string | null;
  actual_source_commit: string | null;
};

/**
 * Parses the file on the backend (whatever parser is registered for scanType) and
 * returns a read-only preview. The caller resubmits the same File to useImportPipeline
 * once the user confirms.
 */
export function useValidateImportPipeline() {
  return useMutation({
    mutationFn: ({
      file,
      projectId,
      bindingId,
      scanType,
    }: {
      file: File;
      projectId: number;
      bindingId: number;
      scanType: string;
    }) => {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("project_id", String(projectId));
      formData.append("binding_id", String(bindingId));
      formData.append("scan_type", scanType);
      return postFormData<ImportPipelinePreview>(getRoute("pipelines_import_validate_url"), formData);
    },
  });
}

export type ImportPipelineResponse = { pipeline_id: string; run_task_id: string };

export function useImportPipeline() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      file,
      projectId,
      bindingId,
      scanType,
    }: {
      file: File;
      projectId: number;
      bindingId: number;
      scanType: string;
    }) => {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("project_id", String(projectId));
      formData.append("binding_id", String(bindingId));
      formData.append("scan_type", scanType);
      return postFormData<ImportPipelineResponse>(getRoute("pipelines_import_url"), formData);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["pipelines"] });
    },
  });
}

export function useValidateOrgIntegration() {
  return useMutation({
    mutationFn: (integrationId: number) =>
      fetchJson<{ task_id: string }>(
        getRoute("org_integration_validate_url", { integration_id: integrationId }),
        { method: "POST" },
      ),
  });
}

export function useExportFinding() {
  return useMutation({
    mutationFn: async ({ findingId }: { findingId: number }) => {
      const resp = await fetchBlob(getRoute("finding_export_url", { finding_id: findingId }), {
        method: "POST",
      });
      const blob = resp.data;
      const filename = getFilenameFromDisposition(resp.headers["content-disposition"] ?? null);
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
