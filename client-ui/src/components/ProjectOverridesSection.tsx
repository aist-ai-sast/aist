import { useState } from "react";

import { toUserMessage } from "../lib/api";
import { useSetProjectIntegrationOverride, useDeleteProjectIntegrationOverride } from "../lib/mutations";
import { useOrgIntegrations, useProjectIntegrationOverrides, useProjects } from "../lib/queries";
import { ORG_INTEGRATION_TYPES, type IntegrationType } from "../lib/integrationTypes";
import { useToast } from "./ToastProvider";
import SelectField from "./SelectField";
import Checkbox from "./Checkbox";
import { TYPE_LABELS, TypeBadge, SectionCard } from "./OrgIntegrationUI";

// Types a project can switch off instead of inheriting the organization default.
const PROJECT_DISABLEABLE_TYPES: IntegrationType[] = ["VPN", "BUILD_ENV"];

export default function ProjectOverridesSection({ orgId }: { orgId: number }) {
  const toast = useToast();
  const projectsQuery = useProjects();
  const integrationsQuery = useOrgIntegrations(orgId);
  const [selectedProjectId, setSelectedProjectId] = useState<number | null>(null);

  const overridesQuery = useProjectIntegrationOverrides(selectedProjectId ?? undefined);
  const setOverride = useSetProjectIntegrationOverride(selectedProjectId ?? 0);
  const deleteOverride = useDeleteProjectIntegrationOverride(selectedProjectId ?? 0);

  const projects = (projectsQuery.data ?? []).filter((project) => project.organizationId === orgId);
  const canOperate = projects.find((project) => project.id === selectedProjectId)?.canOperate ?? false;
  const integrations = integrationsQuery.data ?? [];
  const overrides = overridesQuery.data ?? [];

  function getOverride(type: IntegrationType) {
    return overrides.find((o) => o.integration_type === type) ?? null;
  }

  function getIntegrationsOfType(type: IntegrationType) {
    return integrations.filter((i) => i.integration_type === type);
  }

  async function handleSetOverride(type: IntegrationType, integrationId: number | null) {
    if (!selectedProjectId) return;
    try {
      if (integrationId === null) {
        await deleteOverride.mutateAsync(type);
        toast.push("Override cleared.", "success");
      } else {
        await setOverride.mutateAsync({ integrationType: type, orgIntegrationId: integrationId });
        toast.push("Override saved.", "success");
      }
    } catch (error) {
      toast.push(toUserMessage(error), "error");
    }
  }

  async function handleToggleDisabled(type: IntegrationType, disable: boolean) {
    if (!selectedProjectId) return;
    try {
      if (disable) {
        await setOverride.mutateAsync({ integrationType: type, isDisabled: true });
        toast.push(`${TYPE_LABELS[type]} disabled for this project.`, "success");
      } else {
        await deleteOverride.mutateAsync(type);
        toast.push(`${TYPE_LABELS[type]} override cleared — org default will be used.`, "success");
      }
    } catch (error) {
      toast.push(toUserMessage(error), "error");
    }
  }

  return (
    <SectionCard
      title="Per-Project Overrides"
      description="Override which integration a specific project uses, instead of the org default."
    >
      <SelectField
        label="Project"
        value={selectedProjectId ? String(selectedProjectId) : ""}
        onChange={(value) => setSelectedProjectId(value ? Number(value) : null)}
        placeholder="— select a project —"
        options={projects.map((p) => ({ value: String(p.id), label: p.name }))}
      />

      {selectedProjectId && (
        <div className="space-y-2 pt-1">
          {overridesQuery.isLoading && <div className="text-sm text-slate-400">Loading overrides...</div>}
          {!canOperate && (
            <div className="text-xs text-slate-500">
              You can view this project&apos;s overrides; changing them requires Maintainer or Owner on the project.
            </div>
          )}
          {ORG_INTEGRATION_TYPES.map((type) => {
            const typeIntegrations = getIntegrationsOfType(type);
            const override = getOverride(type);

            if (typeIntegrations.length === 0) return null;

            const isDisabled = override?.is_disabled ?? false;
            const disableToggle = PROJECT_DISABLEABLE_TYPES.includes(type) && (
              <div className="flex flex-wrap items-center gap-2">
                {isDisabled && (
                  <span className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] text-amber-400">
                    disabled for this project
                  </span>
                )}
                <Checkbox
                  label={`Disable ${TYPE_LABELS[type]}`}
                  checked={isDisabled}
                  disabled={!canOperate}
                  onChange={(e) => handleToggleDisabled(type, e.target.checked)}
                />
              </div>
            );

            if (type === "VPN") {
              return (
                <div key={type} className="flex flex-wrap items-center gap-3 rounded-xl border border-night-500/60 bg-night-800/50 px-3 py-2.5">
                  <TypeBadge type={type} />
                  <div className="flex-1 text-sm text-slate-300">VPN</div>
                  {disableToggle}
                </div>
              );
            }

            const currentValue = override?.org_integration ?? null;

            return (
              <div key={type} className="flex flex-wrap items-center gap-3 rounded-xl border border-night-500/60 bg-night-800/50 px-3 py-2.5">
                <TypeBadge type={type} />
                <div className="min-w-0 flex-1">
                  <SelectField
                    label={TYPE_LABELS[type]}
                    hideLabel
                    value={currentValue !== null ? String(currentValue) : ""}
                    onChange={(value) => handleSetOverride(type, Number(value))}
                    placeholder="org default"
                    options={typeIntegrations.map((i) => ({ value: String(i.id), label: i.name }))}
                    disabled={isDisabled || !canOperate}
                  />
                </div>
                {disableToggle}
                {override && !isDisabled && canOperate && (
                  <button
                    className="shrink-0 text-[11px] text-slate-400 hover:text-danger-400 transition"
                    onClick={() => handleSetOverride(type, null)}
                    title="Clear override"
                  >
                    ✕
                  </button>
                )}
              </div>
            );
          })}
          {ORG_INTEGRATION_TYPES.every((type) => getIntegrationsOfType(type).length === 0) && (
            <div className="text-xs text-slate-500">
              No org integrations configured. Add them above first.
            </div>
          )}
        </div>
      )}
    </SectionCard>
  );
}
