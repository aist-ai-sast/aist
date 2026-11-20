import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { toUserMessage } from "../lib/api";
import {
  useDeleteOrgIntegration,
  useDisableDastIntegration,
  useSyncDastCapabilities,
  useValidateOrgIntegration,
  invalidateDastLifecycleQueries,
} from "../lib/mutations";
import {
  useOrgIntegrations,
  useValidationStatus,
  isDastSyncInFlight,
  type OrgIntegration,
} from "../lib/queries";
import { useToast } from "./ToastProvider";
import PermissionGate from "./PermissionGate";
import { SectionCard, AddButton, ResourceRow } from "./OrgIntegrationUI";
import OrgIntegrationForm from "./OrgIntegrationForm";

function variableCount(count: number): string {
  return `${count} ${count === 1 ? "variable" : "variables"}`;
}

export default function OrgIntegrationsSection({ orgId }: { orgId: number }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const integrationsQuery = useOrgIntegrations(orgId);
  const deleteIntegration = useDeleteOrgIntegration(orgId);
  const disableDastIntegration = useDisableDastIntegration(orgId);
  const validateIntegration = useValidateOrgIntegration();
  const syncCapabilities = useSyncDastCapabilities(orgId);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [validatingState, setValidatingState] = useState<{ integrationId: number; taskId: string } | null>(null);
  // DAST integrations whose catalog sync the operator started here (directly or via Validate) and
  // is waiting on; the result toast fires once the backend reports the sync settled.
  const [awaitingSyncIds, setAwaitingSyncIds] = useState<number[]>([]);

  const integrations = integrationsQuery.data ?? [];
  const editingIntegration = integrations.find((i) => i.id === editingId) ?? null;

  const validationStatus = useValidationStatus(
    validatingState?.integrationId ?? null,
    validatingState?.taskId ?? null,
  );

  // Resolve validation result when task completes
  useEffect(() => {
    if (!validatingState) return;
    const s = validationStatus.data?.state;
    if (s === "SUCCESS" || s === "FAILURE" || s === "READY" || s === "INVALID") {
      const data = validationStatus.data!;
      toast.push(
        data.valid
          ? "Credentials are valid."
          : `Validation failed: ${data.detail || "Check integration configuration."}`,
        data.valid ? "success" : "error",
      );
      const { integrationId } = validatingState;
      const isDast = integrations.find((i) => i.id === integrationId)?.integration_type === "DAST";
      setValidatingState(null);
      // A successful DAST validation reserves a catalog sync in the same commit; wait on it too.
      queryClient
        .invalidateQueries({ queryKey: ["org-integrations", orgId] })
        .then(() => { if (isDast && data.valid) awaitSync(integrationId); });
    }
  }, [validationStatus.data?.state]);

  // Resolve awaited DAST catalog syncs once the backend reports them settled
  useEffect(() => {
    if (awaitingSyncIds.length === 0 || !integrationsQuery.data) return;
    const settled = awaitingSyncIds.filter((id) => {
      const integration = integrationsQuery.data.find((i) => i.id === id);
      return !integration || !isDastSyncInFlight(integration);
    });
    if (settled.length === 0) return;
    for (const id of settled) {
      const state = integrationsQuery.data.find((i) => i.id === id)?.dast_state;
      if (state?.sync_status === "SUCCEEDED") {
        toast.push("DAST target catalog synchronized.", "success");
      } else if (state?.sync_status === "FAILED") {
        toast.push(`Synchronization failed: ${state.sync_error_code || "check the DAST integration."}`, "error");
      } else if (state?.sync_status === "STALLED") {
        toast.push("Synchronization did not finish. Try again.", "error");
      }
    }
    setAwaitingSyncIds((ids) => ids.filter((id) => !settled.includes(id)));
    invalidateDastLifecycleQueries(queryClient, orgId);
  }, [integrationsQuery.data, awaitingSyncIds]);

  function awaitSync(integrationId: number) {
    setAwaitingSyncIds((ids) => (ids.includes(integrationId) ? ids : [...ids, integrationId]));
  }

  // First active integration per type is the auto-selected default by resolver
  const defaultIds = new Set<number>();
  const byType = new Map<string, OrgIntegration[]>();
  for (const i of integrations) {
    if (!byType.has(i.integration_type)) byType.set(i.integration_type, []);
    byType.get(i.integration_type)!.push(i);
  }
  for (const group of byType.values()) {
    const firstActive = group.find((i) => i.is_active);
    if (firstActive) defaultIds.add(firstActive.id);
  }

  async function handleDelete(integration: OrgIntegration) {
    const isDast = integration.integration_type === "DAST";
    const warning = isDast
      ? `Delete inactive DAST integration "${integration.name}"? Targets, bindings, presets, schedules, and queue control data will be removed. Pipeline and finding history will be preserved.`
      : `Delete integration "${integration.name}"?`;
    if (!confirm(warning)) return;
    try {
      await deleteIntegration.mutateAsync(integration.id);
      toast.push("Integration deleted.", "success");
    } catch (error) {
      toast.push(toUserMessage(error), "error");
    }
  }

  async function handleDisable(integration: OrgIntegration) {
    if (!confirm(`Disable DAST integration "${integration.name}"? Pipeline history and saved configuration will be preserved, but schedules will be disabled.`)) return;
    try {
      await disableDastIntegration.mutateAsync(integration.id);
      toast.push("DAST integration disabled. History and configuration were preserved.", "success");
    } catch (error) {
      toast.push(toUserMessage(error), "error");
    }
  }

  async function handleValidate(integration: OrgIntegration) {
    try {
      const { task_id } = await validateIntegration.mutateAsync(integration.id);
      setValidatingState({ integrationId: integration.id, taskId: task_id });
    } catch (error) {
      toast.push(toUserMessage(error), "error");
    }
  }

  async function handleSynchronize(integration: OrgIntegration) {
    try {
      await syncCapabilities.mutateAsync(integration.id);
      awaitSync(integration.id);
    } catch (error) {
      toast.push(toUserMessage(error), "error");
    }
  }

  function handleDone() {
    setShowForm(false);
    setEditingId(null);
  }

  return (
    <SectionCard
      title="Org Integrations"
      description="GitLab, GitHub, Gerrit, Gitea, Slack, and Email accounts and credentials shared across all projects. The first active integration per type is used by default."
    >
      {integrationsQuery.isLoading && <div className="text-sm text-slate-400">Loading...</div>}
      {integrationsQuery.isError && (
        <div className="text-sm text-danger-400">{toUserMessage(integrationsQuery.error)}</div>
      )}
      {integrations.length === 0 && !integrationsQuery.isLoading && (
        <div className="rounded-2xl border border-night-500/40 bg-night-800/40 px-4 py-5 text-center text-sm text-slate-400">
          No integrations configured.
        </div>
      )}
      {integrations.map((integration) =>
        editingId === integration.id ? (
          <OrgIntegrationForm key={integration.id} orgId={orgId} editing={editingIntegration} onDone={handleDone} />
        ) : (
          <ResourceRow
            key={integration.id}
            typeKey={integration.integration_type}
            name={integration.name}
            hasSecret={integration.has_secret}
            isActive={integration.is_active}
            isDefault={defaultIds.has(integration.id)}
            vpnName={
              integration.vpn_integration != null
                ? integrations.find((i) => i.id === integration.vpn_integration)?.name
                : undefined
            }
            onEdit={() => { setEditingId(integration.id); setShowForm(false); }}
            onDelete={() =>
              integration.integration_type === "DAST" && integration.is_active
                ? handleDisable(integration)
                : handleDelete(integration)
            }
            onValidate={() => handleValidate(integration)}
            onSynchronize={
              integration.integration_type === "DAST" && integration.dast_state?.validation_state === "READY"
                ? () => handleSynchronize(integration)
                : undefined
            }
            isPendingDelete={deleteIntegration.isPending || disableDastIntegration.isPending}
            isPendingValidate={
              (validateIntegration.isPending && validateIntegration.variables === integration.id) ||
              validatingState?.integrationId === integration.id
            }
            isPendingSynchronize={
              (syncCapabilities.isPending && syncCapabilities.variables === integration.id) ||
              isDastSyncInFlight(integration)
            }
            organizationId={orgId}
            destructiveLabel={
              integration.integration_type === "DAST" && integration.is_active ? "Disable" : "Delete"
            }
            destructiveTitle={
              integration.integration_type === "DAST" && integration.is_active
                ? "Disable DAST integration"
                : "Delete integration"
            }
            statusLabel={
              integration.integration_type === "DAST"
                ? integration.dast_state?.validation_state.replaceAll("_", " ")
                : integration.integration_type === "BUILD_ENV"
                  ? variableCount(integration.build_env?.length ?? 0)
                  : undefined
            }
            fingerprint={
              integration.integration_type === "DAST"
                ? String(integration.config.server_fingerprint ?? "")
                : undefined
            }
          />
        ),
      )}
      {showForm && !editingId ? (
        <OrgIntegrationForm orgId={orgId} editing={null} onDone={handleDone} />
      ) : !editingId ? (
        <PermissionGate action="manage_access" organizationId={orgId}>
          <AddButton onClick={() => setShowForm(true)} />
        </PermissionGate>
      ) : null}
    </SectionCard>
  );
}
