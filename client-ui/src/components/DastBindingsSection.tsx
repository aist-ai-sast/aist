import { useState } from "react";
import type { IChangeEvent } from "@rjsf/core";
import type { RJSFSchema } from "@rjsf/utils";

import { toUserMessage } from "../lib/api";
import { targetRequiresSourceRepository } from "../lib/dast";
import {
  useDeleteDastProjectBinding,
  useUpsertDastProjectBinding,
  type DastBindingPayload,
} from "../lib/mutations";
import {
  useOrganizationDastTargets,
  useOrgIntegrations,
  useProjectDastBindings,
  useProjects,
  type DastProjectBinding,
  type DastTarget,
} from "../lib/queries";
import { useToast } from "./ToastProvider";
import Checkbox from "./Checkbox";
import SchemaForm from "./SchemaForm";
import SelectField from "./SelectField";
import PermissionGate from "./PermissionGate";
import { SectionCard, TypeBadge, AddButton } from "./OrgIntegrationUI";

type BindingFormState = {
  targetId: number;
  sourceRepoKey: string;
  enabled: boolean;
  parameters: Record<string, unknown>;
};

function initialBindingForm(target: DastTarget): BindingFormState {
  return {
    targetId: target.id,
    sourceRepoKey: targetRequiresSourceRepository(target) ? target.repository_keys[0] ?? "" : "",
    enabled: true,
    parameters: structuredClone(target.provider_defaults),
  };
}

function DastBindingsSectionContent({ orgId }: { orgId: number }) {
  const toast = useToast();
  const projectsQuery = useProjects();
  const targetsQuery = useOrganizationDastTargets(orgId);
  const [selectedProjectId, setSelectedProjectId] = useState<number | null>(null);
  const [editingBindingId, setEditingBindingId] = useState<number | null>(null);
  const [formState, setFormState] = useState<BindingFormState | null>(null);
  const bindingsQuery = useProjectDastBindings(selectedProjectId ?? undefined);
  const upsertBinding = useUpsertDastProjectBinding(selectedProjectId ?? 0);
  const deleteBinding = useDeleteDastProjectBinding(selectedProjectId ?? 0);

  const projects = (projectsQuery.data ?? []).filter((project) => project.organizationId === orgId);
  const targets = (targetsQuery.data ?? []).filter((target) => target.is_available);
  const bindings = bindingsQuery.data ?? [];
  const selectedTarget = formState
    ? targets.find((target) => target.id === formState.targetId) ?? null
    : null;

  function resetForm() {
    setEditingBindingId(null);
    setFormState(null);
  }

  function startCreate() {
    const boundIds = new Set(bindings.map((binding) => binding.target.id));
    const target = targets.find((candidate) => !boundIds.has(candidate.id));
    if (!target) {
      toast.push("Every available DAST target is already bound to this project.", "error");
      return;
    }
    setEditingBindingId(null);
    setFormState(initialBindingForm(target));
  }

  function startEdit(binding: DastProjectBinding) {
    setEditingBindingId(binding.id);
    setFormState({
      targetId: binding.target.id,
      sourceRepoKey: binding.source_repo_key,
      enabled: binding.enabled,
      parameters: structuredClone(binding.parameter_snapshot),
    });
  }

  function selectTarget(targetId: number) {
    const target = targets.find((candidate) => candidate.id === targetId);
    if (target) setFormState(initialBindingForm(target));
  }

  async function submitBinding(event: IChangeEvent<Record<string, unknown>>) {
    if (!selectedTarget || !formState) return;
    const payload: DastBindingPayload = {
      target_id: selectedTarget.id,
      capability_revision: selectedTarget.capability_revision,
      schema_digest: selectedTarget.schema_digest,
      source_repo_key: formState.sourceRepoKey,
      enabled: formState.enabled,
      parameter_snapshot: event.formData ?? {},
    };
    try {
      await upsertBinding.mutateAsync(payload);
      toast.push(editingBindingId ? "DAST binding updated." : "DAST binding created.", "success");
      resetForm();
    } catch (error) {
      toast.push(toUserMessage(error), "error");
    }
  }

  async function removeBinding(bindingId: number) {
    try {
      await deleteBinding.mutateAsync(bindingId);
      toast.push("DAST binding removed.", "success");
      if (editingBindingId === bindingId) resetForm();
    } catch (error) {
      toast.push(toUserMessage(error), "error");
    }
  }

  return (
    <SectionCard
      title="DAST Target Bindings"
      description="Bind one or more synchronized DAST targets to a project. Parameters come directly from the provider schema."
    >
      <SelectField
        label="Project"
        value={selectedProjectId ? String(selectedProjectId) : ""}
        onChange={(value) => {
          setSelectedProjectId(value ? Number(value) : null);
          resetForm();
        }}
        placeholder="— select a project —"
        options={projects.map((project) => ({ value: String(project.id), label: project.name }))}
      />

      {selectedProjectId && (
        <div className="space-y-3 pt-1">
          {bindingsQuery.isLoading && <div className="text-sm text-slate-400">Loading DAST bindings...</div>}
          {bindings.map((binding) => (
            <div key={binding.id} className="rounded-xl border border-night-500/60 bg-night-800/50 px-3 py-3">
              <div className="flex items-start gap-3">
                <TypeBadge type="DAST" />
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium text-slate-200">{binding.target.display_name}</div>
                  {/*
                    Provider digests (schema_digest / capability_revision) are launch-admission
                    machinery, not operator-facing facts — the backend already fails a launch when
                    they drift. Showing them here only added noise nobody could act on.
                  */}
                  {targetRequiresSourceRepository(binding.target) ? (
                    <div className="mt-1 text-xs text-slate-400">
                      Source repository: {binding.source_repo_key}
                    </div>
                  ) : null}
                  <div className="mt-1 text-[11px] text-slate-500">
                    {binding.enabled ? "Enabled" : "Disabled"}
                    {!binding.target.is_available ? " · target unavailable" : ""}
                  </div>
                  {binding.readiness.ready ? (
                    <div className="mt-2 text-xs text-emerald-300">Ready to launch</div>
                  ) : (
                    <ul className="mt-2 space-y-1 text-xs text-amber-300">
                      {binding.readiness.issues.map((issue) => (
                        <li key={issue.code}>{issue.detail}</li>
                      ))}
                    </ul>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <button className="text-xs text-brand-300 hover:text-brand-200" onClick={() => startEdit(binding)}>
                    Edit
                  </button>
                  <button className="text-xs text-danger-400 hover:text-danger-300" onClick={() => removeBinding(binding.id)}>
                    Remove
                  </button>
                </div>
              </div>
            </div>
          ))}

          {!bindingsQuery.isLoading && bindings.length === 0 && !formState && (
            <div className="text-xs text-slate-500">No DAST targets are bound to this project.</div>
          )}

          {formState && selectedTarget && (
            <div className="space-y-3 rounded-xl border border-brand-500/30 bg-night-800/70 p-4">
              {/*
                Binding metadata is ours, not the provider's, so its controls stay outside the
                schema form even though both now render through the same components.
              */}
              {!editingBindingId && (
                <SelectField
                  label="DAST target"
                  value={String(formState.targetId)}
                  onChange={(value) => selectTarget(Number(value))}
                  options={targets
                    .filter((target) => !bindings.some((binding) => binding.target.id === target.id))
                    .map((target) => ({ value: String(target.id), label: target.display_name }))}
                />
              )}
              {targetRequiresSourceRepository(selectedTarget) && (
                <SelectField
                  label="Source repository"
                  value={formState.sourceRepoKey}
                  onChange={(value) => setFormState({ ...formState, sourceRepoKey: value })}
                  options={selectedTarget.repository_keys.map((key) => ({ value: key, label: key }))}
                />
              )}
              <Checkbox
                label="Enabled"
                checked={formState.enabled}
                onChange={(event) => setFormState({ ...formState, enabled: event.target.checked })}
              />
              <SchemaForm
                formKey={`${selectedTarget.id}:${selectedTarget.schema_digest}:${editingBindingId ?? "new"}`}
                schema={selectedTarget.parameter_schema as RJSFSchema}
                formData={formState.parameters}
                onChange={(event) => setFormState((current) => current ? { ...current, parameters: event.formData ?? {} } : current)}
                onSubmit={submitBinding}
              >
                <div className="flex justify-end gap-2 pt-1">
                  <button type="button" className="rounded-lg px-3 py-2 text-xs text-slate-300 hover:text-white" onClick={resetForm}>
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={
                      upsertBinding.isPending ||
                      (targetRequiresSourceRepository(selectedTarget) && !formState.sourceRepoKey)
                    }
                    className="rounded-lg bg-brand-500 px-3 py-2 text-xs font-medium text-white disabled:opacity-50"
                  >
                    {upsertBinding.isPending ? "Saving..." : "Save binding"}
                  </button>
                </div>
              </SchemaForm>
            </div>
          )}

          {!formState && targets.length > 0 && <AddButton onClick={startCreate} />}
          {!targetsQuery.isLoading && targets.length === 0 && (
            <div className="text-xs text-slate-500">No available DAST targets. Validate and synchronize the organization integration first.</div>
          )}
        </div>
      )}
    </SectionCard>
  );
}

/**
 * Binding management is a mutating, per-project action — `PermissionGate` only resolves
 * org-scoped permissions (`usePermissions(organizationId)`), so this gates the whole card
 * rather than only the buttons inside it: a reader with no operate rights on the org gets
 * an explanatory notice instead of a read-only shell of a section they can't act on.
 */
export default function DastBindingsSection({ orgId }: { orgId: number }) {
  const integrationsQuery = useOrgIntegrations(orgId);
  const hasDastIntegration = (integrationsQuery.data ?? []).some(
    (integration) => integration.integration_type === "DAST",
  );

  if (!hasDastIntegration) return null;

  return (
    <PermissionGate
      action="operate_project"
      organizationId={orgId}
      fallback={
        <SectionCard title="DAST Target Bindings">
          <p className="text-xs text-slate-500">
            Binding management is available to the organization administrator.
          </p>
        </SectionCard>
      }
    >
      <DastBindingsSectionContent orgId={orgId} />
    </PermissionGate>
  );
}
