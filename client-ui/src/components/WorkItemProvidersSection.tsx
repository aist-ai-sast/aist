import { useEffect, useState } from "react";

import { toUserMessage } from "../lib/api";
import {
  useCreateWorkItemProvider,
  useDeleteWorkItemProvider,
  useUpdateWorkItemProvider,
  useValidateWorkItemProvider,
  type WorkItemProviderPayload,
} from "../lib/mutations";
import {
  useOrgIntegrations,
  useWorkItemProviders,
  useWorkItemProviderValidationStatus,
  type WorkItemProviderSummary,
} from "../lib/queries";
import { useToast } from "./ToastProvider";
import SelectField from "./SelectField";
import TextInput from "./TextInput";
import PasswordField from "./PasswordField";
import PermissionGate from "./PermissionGate";
import { TYPE_LABELS, SectionCard, AddButton, ResourceRow, SaveCancelButtons } from "./OrgIntegrationUI";

const WI_PROVIDER_TYPES = ["JIRA", "YOUTRACK", "GITHUB", "GITLAB", "LINEAR", "AZURE_DEVOPS", "GENERIC"] as const;

type WIProviderFormState = {
  provider_type: string;
  name: string;
  base_url: string;
  api_token: string;
  jira_email: string;
  sync_enabled: boolean;
  is_active: boolean;
  vpn_integration: number | null;
};

const EMPTY_WI_FORM: WIProviderFormState = {
  provider_type: "JIRA",
  name: "",
  base_url: "",
  api_token: "",
  jira_email: "",
  sync_enabled: true,
  is_active: true,
  vpn_integration: null,
};

function WorkItemProviderForm({
  orgId,
  editing,
  onDone,
}: {
  orgId: number;
  editing: WorkItemProviderSummary | null;
  onDone: () => void;
}) {
  const toast = useToast();
  const createProvider = useCreateWorkItemProvider(orgId);
  const updateProvider = useUpdateWorkItemProvider(orgId);
  const { data: orgIntegrations } = useOrgIntegrations(orgId);
  const vpnOptions = (orgIntegrations ?? []).filter((i) => i.integration_type === "VPN" && i.is_active);

  const [form, setForm] = useState<WIProviderFormState>(() =>
    editing
      ? {
          provider_type: editing.providerType,
          name: editing.name,
          base_url: editing.baseUrl,
          api_token: "",
          jira_email: "",
          sync_enabled: editing.syncEnabled,
          is_active: true,
          vpn_integration: editing.vpnIntegrationId,
        }
      : EMPTY_WI_FORM,
  );

  useEffect(() => {
    setForm(
      editing
        ? {
            provider_type: editing.providerType,
            name: editing.name,
            base_url: editing.baseUrl,
            api_token: "",
            jira_email: "",
            sync_enabled: editing.syncEnabled,
            is_active: true,
            vpn_integration: editing.vpnIntegrationId,
          }
        : EMPTY_WI_FORM,
    );
  }, [editing]);

  async function handleSave() {
    const payload: WorkItemProviderPayload = {
      provider_type: form.provider_type,
      name: form.name.trim(),
      sync_enabled: form.sync_enabled,
      is_active: form.is_active,
      vpn_integration: form.vpn_integration,
    };
    if (form.base_url.trim()) payload.base_url = form.base_url.trim();
    if (form.api_token.trim()) payload.api_token = form.api_token.trim();
    if (form.provider_type === "JIRA" && form.jira_email.trim()) {
      payload.provider_config = { jira_email: form.jira_email.trim() };
    }
    try {
      if (editing) {
        await updateProvider.mutateAsync({ providerId: editing.id, payload });
        toast.push("Provider updated.", "success");
      } else {
        await createProvider.mutateAsync(payload);
        toast.push("Provider created.", "success");
      }
      onDone();
    } catch (error) {
      toast.push(toUserMessage(error), "error");
    }
  }

  const isPending = createProvider.isPending || updateProvider.isPending;
  const needsBaseUrl = form.provider_type !== "GENERIC";

  return (
    <div className="rounded-2xl border border-night-500/80 bg-night-700/60 p-4 space-y-3">
      <div className="text-xs uppercase tracking-[0.2em] text-slate-400">
        {editing ? "Edit Provider" : "New Work Item Provider"}
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="sm:col-span-2">
          <SelectField
            label="Type"
            value={form.provider_type}
            onChange={(value) => setForm((prev) => ({ ...prev, provider_type: value }))}
            options={WI_PROVIDER_TYPES.map((t) => ({ value: t, label: TYPE_LABELS[t] ?? t }))}
            disabled={!!editing}
          />
        </div>
        <label className="text-xs text-slate-400 sm:col-span-2">
          Name
          <TextInput
            className="mt-1"
            placeholder="e.g. Production"
            value={form.name}
            onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))}
          />
        </label>
        {needsBaseUrl && (
          <label className="text-xs text-slate-400 sm:col-span-2">
            Base URL
            {form.provider_type === "JIRA" && (
              <span className="ml-1 text-slate-500">
                — instance root only, e.g. <code className="break-all text-slate-300">https://company.atlassian.net</code>
                {" "}<span className="text-danger-400/70">(not the project URL)</span>
              </span>
            )}
            <TextInput
              className="mt-1"
              placeholder={form.provider_type === "JIRA" ? "https://company.atlassian.net" : "https://your-instance.example.com"}
              value={form.base_url}
              onChange={(e) => setForm((prev) => ({ ...prev, base_url: e.target.value }))}
            />
          </label>
        )}
        {form.provider_type === "JIRA" && (
          <label className="text-xs text-slate-400 sm:col-span-2">
            Jira Email
            <span className="ml-1 text-slate-500">(required for Jira Cloud — leave blank for Data Center PAT)</span>
            <TextInput
              className="mt-1"
              placeholder="you@company.com"
              value={form.jira_email}
              onChange={(e) => setForm((prev) => ({ ...prev, jira_email: e.target.value }))}
            />
          </label>
        )}
        <label className="text-xs text-slate-400 sm:col-span-2">
          API Token
          {editing?.hasToken && <span className="ml-1 text-slate-500">(leave blank to keep existing)</span>}
          <PasswordField
            className="mt-1"
            placeholder={editing?.hasToken ? "••••••••••••••••" : ""}
            value={form.api_token}
            onChange={(e) => setForm((prev) => ({ ...prev, api_token: e.target.value }))}
          />
        </label>
        <div className="sm:col-span-2">
          <SelectField
            label="VPN Integration"
            value={form.vpn_integration !== null ? String(form.vpn_integration) : ""}
            onChange={(value) =>
              setForm((prev) => ({ ...prev, vpn_integration: value ? Number(value) : null }))
            }
            placeholder="None"
            clearable
            clearLabel="None"
            options={vpnOptions.map((i) => ({ value: String(i.id), label: i.name }))}
          />
          {vpnOptions.length === 0 && (
            <p className="mt-1 text-[11px] text-slate-500">
              No active VPN integrations configured for this organization.
            </p>
          )}
        </div>
        <div className="flex gap-6 sm:col-span-2">
          <label className="flex items-center gap-2 text-xs text-slate-400">
            <input
              type="checkbox"
              className="accent-brand-500"
              checked={form.sync_enabled}
              onChange={(e) => setForm((prev) => ({ ...prev, sync_enabled: e.target.checked }))}
            />
            Sync enabled
          </label>
          <label className="flex items-center gap-2 text-xs text-slate-400">
            <input
              type="checkbox"
              className="accent-brand-500"
              checked={form.is_active}
              onChange={(e) => setForm((prev) => ({ ...prev, is_active: e.target.checked }))}
            />
            Active
          </label>
        </div>
      </div>
      <SaveCancelButtons
        onSave={handleSave}
        onCancel={onDone}
        isPending={isPending}
        disabled={!form.name.trim()}
        label={editing ? "Save changes" : "Create"}
      />
    </div>
  );
}

export default function WorkItemProvidersSection({ orgId }: { orgId: number }) {
  const toast = useToast();
  const providersQuery = useWorkItemProviders(orgId);
  const deleteProvider = useDeleteWorkItemProvider(orgId);
  const validateProvider = useValidateWorkItemProvider();
  const { data: orgIntegrations } = useOrgIntegrations(orgId);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [validatingProviderState, setValidatingProviderState] = useState<{ providerId: number; taskId: string } | null>(null);

  const providers = providersQuery.data ?? [];
  const editingProvider = providers.find((p) => p.id === editingId) ?? null;

  const providerValidationStatus = useWorkItemProviderValidationStatus(
    validatingProviderState?.providerId ?? null,
    validatingProviderState?.taskId ?? null,
  );

  useEffect(() => {
    if (!validatingProviderState) return;
    const s = providerValidationStatus.data?.state;
    if (s === "SUCCESS" || s === "FAILURE") {
      const data = providerValidationStatus.data!;
      toast.push(
        data.valid
          ? "Provider credentials are valid."
          : `Validation failed: ${data.detail || "Check provider configuration."}`,
        data.valid ? "success" : "error",
      );
      setValidatingProviderState(null);
    }
  }, [providerValidationStatus.data?.state]);

  async function handleDelete(provider: WorkItemProviderSummary) {
    if (!confirm(`Delete provider "${provider.name}"?`)) return;
    try {
      await deleteProvider.mutateAsync(provider.id);
      toast.push("Provider deleted.", "success");
    } catch (error) {
      toast.push(toUserMessage(error), "error");
    }
  }

  async function handleValidate(provider: WorkItemProviderSummary) {
    try {
      const { task_id } = await validateProvider.mutateAsync(provider.id);
      setValidatingProviderState({ providerId: provider.id, taskId: task_id });
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
      title="Work Item Providers"
      description="Connections to Jira, YouTrack, Linear, Azure DevOps, and other issue trackers."
    >
      {providersQuery.isLoading && <div className="text-sm text-slate-400">Loading...</div>}
      {providers.length === 0 && !providersQuery.isLoading && (
        <div className="rounded-2xl border border-night-500/40 bg-night-800/40 px-4 py-5 text-center text-sm text-slate-400">
          No work item providers configured.
        </div>
      )}
      {providers.map((provider) =>
        editingId === provider.id ? (
          <WorkItemProviderForm key={provider.id} orgId={orgId} editing={editingProvider} onDone={handleDone} />
        ) : (
          <ResourceRow
            key={provider.id}
            typeKey={provider.providerType}
            name={provider.name}
            hasSecret={provider.hasToken}
            isActive={true}
            vpnName={
              provider.vpnIntegrationId != null
                ? (orgIntegrations ?? []).find((i) => i.id === provider.vpnIntegrationId)?.name
                : undefined
            }
            onEdit={() => { setEditingId(provider.id); setShowForm(false); }}
            onDelete={() => handleDelete(provider)}
            onValidate={() => handleValidate(provider)}
            isPendingDelete={deleteProvider.isPending}
            isPendingValidate={
              (validateProvider.isPending && validateProvider.variables === provider.id) ||
              validatingProviderState?.providerId === provider.id
            }
            organizationId={orgId}
          />
        ),
      )}
      {showForm && !editingId ? (
        <WorkItemProviderForm orgId={orgId} editing={null} onDone={handleDone} />
      ) : !editingId ? (
        <PermissionGate action="manage_access" organizationId={orgId}>
          <AddButton onClick={() => setShowForm(true)} />
        </PermissionGate>
      ) : null}
    </SectionCard>
  );
}
