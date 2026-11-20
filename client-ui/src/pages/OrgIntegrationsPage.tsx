import { useManageableOrgs } from "../lib/queries";
import OrgIntegrationsSection from "../components/OrgIntegrationsSection";
import WorkItemProvidersSection from "../components/WorkItemProvidersSection";
import ProjectOverridesSection from "../components/ProjectOverridesSection";
import DastBindingsSection from "../components/DastBindingsSection";

export default function OrgIntegrationsPage() {
  const manageableOrgsQuery = useManageableOrgs();

  const orgs = manageableOrgsQuery.data ?? [];

  if (manageableOrgsQuery.isLoading) {
    return (
      <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
        Loading...
      </div>
    );
  }

  if (orgs.length === 0) {
    return (
      <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-400">
        You need Maintainer or Owner role in an organization to manage integrations.
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-white">Integrations</h1>
        <p className="mt-1 text-xs text-slate-400">
          Manage org-level credentials for source control, notifications, and issue trackers.
        </p>
      </div>

      {orgs.map((org) => (
        <div key={org.id} className="space-y-4">
          {orgs.length > 1 && (
            <div className="text-xs uppercase tracking-[0.2em] text-slate-400 pt-2">
              {org.name}
            </div>
          )}
          <OrgIntegrationsSection orgId={org.id} />
          <WorkItemProvidersSection orgId={org.id} />
          <ProjectOverridesSection orgId={org.id} />
          <DastBindingsSection orgId={org.id} />
        </div>
      ))}
    </div>
  );
}
