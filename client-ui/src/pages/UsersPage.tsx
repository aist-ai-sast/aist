import { useEffect, useMemo, useState } from "react";

import { ObjectIcons } from "../components/ObjectIcons";
import PageErrorState from "../components/PageErrorState";
import ProjectAccessEditor, { type RoleByProject } from "../components/ProjectAccessEditor";
import SelectField from "../components/SelectField";
import TextInput from "../components/TextInput";
import { useToast } from "../components/ToastProvider";
import { toUserMessage } from "../lib/api";
import { getRoleBadgeClass, getRoleIcon } from "../lib/roleBadge";
import { useManageableOrgs, useProjects } from "../lib/queries";
import {
  ROLE_OPTIONS,
  type OrgMember,
  inviteResultMessage,
  useChangeMemberRole,
  useGrantProject,
  useInviteMember,
  useOrgMembers,
  useRemoveMember,
  useResetMemberPassword,
  useResetOrgMemberAccess,
  useRevokeProject,
} from "../lib/orgMembers";

const roleSelectOptions = ROLE_OPTIONS.map((role) => ({ value: String(role.id), label: role.name }));

// A full member has no per-project grants by default — they see every
// project at their org-wide role. Seeded as the baseline, then per-project
// grants (a capped downgrade for a full member, or the sole allow-list source
// for a restricted member) and explicit denials override individual rows —
// touching one project never affects any other project's row. Exported for
// direct testing.
export function buildRoleByProject(
  member: Pick<OrgMember, "membership_type" | "role_id" | "project_grants" | "denied_project_ids">,
  orgProjects: { id: number }[],
): RoleByProject {
  const map: RoleByProject = {};
  if (member.membership_type === "full") {
    orgProjects.forEach((project) => { map[project.id] = member.role_id; });
  }
  member.project_grants.forEach((grant) => { map[grant.project_id] = grant.role_id; });
  member.denied_project_ids.forEach((projectId) => { map[projectId] = null; });
  return map;
}

export default function UsersPage() {
  const orgsQuery = useManageableOrgs();
  const [orgId, setOrgId] = useState<number | null>(null);

  useEffect(() => {
    if (orgId === null && orgsQuery.data && orgsQuery.data.length) {
      setOrgId(orgsQuery.data[0].id);
    }
  }, [orgId, orgsQuery.data]);

  if (orgsQuery.isError) {
    return <PageErrorState error={orgsQuery.error} fallbackTitle="Failed to load organizations" />;
  }
  if (!orgsQuery.isLoading && (!orgsQuery.data || !orgsQuery.data.length)) {
    return (
      <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
        You do not manage any organizations.
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="text-xs uppercase tracking-[0.2em] text-slate-400">User Management</div>
          <h1 className="mt-2 flex items-center gap-2 text-2xl font-semibold text-white">
            <span className="text-brand-200">{ObjectIcons.users}</span>
            Users
          </h1>
          <p className="mt-1 text-xs text-slate-400">Invite members, assign roles and per-project access.</p>
        </div>
        {orgsQuery.data && orgsQuery.data.length > 1 ? (
          <div className="w-64">
            <SelectField
              label="Organization"
              value={orgId ? String(orgId) : ""}
              onChange={(value) => setOrgId(value ? Number(value) : null)}
              options={orgsQuery.data.map((org) => ({ value: String(org.id), label: org.name }))}
            />
          </div>
        ) : null}
      </div>

      {orgId ? <OrgUsers orgId={orgId} /> : null}
    </div>
  );
}

function OrgUsers({ orgId }: { orgId: number }) {
  const membersQuery = useOrgMembers(orgId);
  const [accessMember, setAccessMember] = useState<OrgMember | null>(null);

  if (membersQuery.isError) {
    return <PageErrorState error={membersQuery.error} fallbackTitle="Failed to load members" />;
  }

  const members = membersQuery.data ?? [];
  const activeMember = accessMember
    ? members.find((m) => m.user_id === accessMember.user_id) ?? accessMember
    : null;

  return (
    <div className="space-y-4">
      <InvitePanel orgId={orgId} />

      <section className="aist-card border-night-500/80 p-0">
        <div className="border-b border-night-500/70 px-5 py-4 text-xs uppercase tracking-[0.2em] text-slate-300">
          Members
        </div>
        <div className="px-5 py-4">
          {membersQuery.isLoading ? (
            <p className="text-xs text-slate-400">Loading members...</p>
          ) : members.length ? (
            <div className="space-y-2">
              {members.map((member) => (
                <MemberRow
                  key={member.user_id}
                  orgId={orgId}
                  member={member}
                  onManageAccess={() => setAccessMember(member)}
                />
              ))}
            </div>
          ) : (
            <p className="text-xs text-slate-400">No members yet. Invite someone to get started.</p>
          )}
        </div>
      </section>

      {activeMember ? (
        <AccessDrawer orgId={orgId} member={activeMember} onClose={() => setAccessMember(null)} />
      ) : null}
    </div>
  );
}

function InvitePanel({ orgId }: { orgId: number }) {
  const invite = useInviteMember(orgId);
  const projectsQuery = useProjects();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [mode, setMode] = useState<"full" | "restricted">("full");
  const [roleId, setRoleId] = useState("5");
  const [roleByProject, setRoleByProject] = useState<RoleByProject>({});

  const orgProjects = useMemo(
    () => (projectsQuery.data ?? [])
      .filter((project) => project.organizationId === orgId)
      .map((project) => ({ id: project.id, name: project.name })),
    [projectsQuery.data, orgId],
  );

  function reset() {
    setEmail("");
    setMode("full");
    setRoleId("5");
    setRoleByProject({});
    setOpen(false);
  }

  async function submit() {
    if (!email.trim()) {
      toast.push("Enter an email.", "error");
      return;
    }
    const payload = { email: email.trim() } as { email: string; role_id?: number; project_grants?: { project_id: number; role_id: number }[] };
    if (mode === "full") {
      payload.role_id = Number(roleId);
    } else {
      const grants = Object.entries(roleByProject)
        .filter(([, role]) => role != null)
        .map(([projectId, role]) => ({ project_id: Number(projectId), role_id: Number(role) }));
      if (!grants.length) {
        toast.push("Grant access to at least one project, or choose full membership.", "error");
        return;
      }
      payload.project_grants = grants;
    }
    try {
      const result = await invite.mutateAsync(payload);
      toast.push(inviteResultMessage(result), "success");
      reset();
    } catch (error) {
      toast.push(toUserMessage(error), "error");
    }
  }

  if (!open) {
    return (
      <button className="aist-icon-button" onClick={() => setOpen(true)}>
        <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
          <path fill="currentColor" d="M11 11V5h2v6h6v2h-6v6h-2v-6H5v-2z" />
        </svg>
        Invite member
      </button>
    );
  }

  return (
    <section className="aist-card border-night-500/80 p-5 space-y-4">
      <div className="grid gap-3 sm:grid-cols-[1fr_14rem] sm:items-end">
        <label className="text-xs text-slate-400">
          Email
          <TextInput
            variant="password"
            type="text"
            inputMode="email"
            className="mt-1"
            placeholder="member@example.com"
            value={email}
            disabled={invite.isPending}
            onChange={(event) => setEmail(event.target.value)}
          />
        </label>
        <SelectField
          label="Access"
          value={mode}
          onChange={(value) => setMode(value as "full" | "restricted")}
          options={[
            { value: "full", label: "Full member (all projects)" },
            { value: "restricted", label: "Specific projects only" },
          ]}
          disabled={invite.isPending}
        />
      </div>

      {mode === "full" ? (
        <div className="sm:max-w-xs">
          <SelectField
            label="Organization role"
            value={roleId}
            onChange={setRoleId}
            options={roleSelectOptions}
            disabled={invite.isPending}
          />
        </div>
      ) : (
        <div>
          <div className="mb-2 text-xs text-slate-400">Choose which projects this member can access and their role on each.</div>
          <ProjectAccessEditor
            projects={orgProjects}
            roleByProject={roleByProject}
            disabled={invite.isPending}
            onSetRole={(projectId, role) => setRoleByProject((prev) => ({ ...prev, [projectId]: role }))}
          />
        </div>
      )}

      <div className="flex gap-2">
        <button className="aist-icon-button" disabled={invite.isPending} onClick={submit}>
          Send invite
        </button>
        <button
          className="aist-icon-button border-night-400/80 bg-night-800/80 text-slate-200"
          disabled={invite.isPending}
          onClick={reset}
        >
          Cancel
        </button>
      </div>
      <p className="text-xs text-slate-400">The member receives an email link to set their own password.</p>
    </section>
  );
}

function MemberRow({
  orgId,
  member,
  onManageAccess,
}: {
  orgId: number;
  member: OrgMember;
  onManageAccess: () => void;
}) {
  const changeRole = useChangeMemberRole(orgId);
  const removeMember = useRemoveMember(orgId);
  const resetPassword = useResetMemberPassword(orgId);
  const toast = useToast();

  const displayName = [member.first_name, member.last_name].filter(Boolean).join(" ") || member.username;
  const isFull = member.membership_type === "full";

  return (
    <div className="rounded-2xl border border-night-500/80 bg-night-800/75 px-3.5 py-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium text-slate-100">{displayName}</span>
            {member.has_token ? (
              <span
                title={`${member.token_count} API token(s)`}
                className="inline-flex items-center gap-1 rounded-full border border-brand-500/40 bg-brand-500/10 px-2 py-0.5 text-[10px] uppercase tracking-wide text-brand-100"
              >
                Token
              </span>
            ) : null}
            {member.is_active ? null : (
              <span className="rounded-full border border-slate-500/40 bg-slate-500/10 px-2 py-0.5 text-[10px] uppercase text-slate-400">
                Inactive
              </span>
            )}
          </div>
          <div className="mt-0.5 truncate text-[11px] text-slate-400">{member.email || member.username}</div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {isFull ? (
            <div className="w-40">
              <SelectField
                label="Role"
                hideLabel
                value={member.role_id ? String(member.role_id) : ""}
                disabled={changeRole.isPending}
                onChange={async (value) => {
                  try {
                    await changeRole.mutateAsync({ userId: member.user_id, roleId: Number(value) });
                    toast.push("Role updated.", "success");
                  } catch (error) {
                    toast.push(toUserMessage(error), "error");
                  }
                }}
                options={roleSelectOptions}
              />
            </div>
          ) : (
            <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-medium ${getRoleBadgeClass(member.role_name)}`}>
              {getRoleIcon(member.role_name)}
              <span>Restricted · {member.project_grants.length} project(s)</span>
            </span>
          )}

          <button className="aist-icon-button" onClick={onManageAccess}>
            Manage access
          </button>
          <button
            className="aist-icon-button"
            disabled={resetPassword.isPending}
            onClick={async () => {
              try {
                await resetPassword.mutateAsync(member.user_id);
                toast.push(`Password reset email sent to ${member.email || member.username}. They can use the link to choose a new password.`, "success");
              } catch (error) {
                toast.push(toUserMessage(error), "error");
              }
            }}
          >
            Reset password
          </button>
          <button
            className="inline-flex items-center gap-2 rounded-xl border border-danger-500/70 bg-danger-500/10 px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.09em] text-danger-500 transition hover:bg-danger-500/20 disabled:opacity-60"
            disabled={removeMember.isPending}
            onClick={async () => {
              if (!window.confirm(`Remove ${displayName} from this organization?`)) return;
              try {
                await removeMember.mutateAsync(member.user_id);
                toast.push("Member removed.", "success");
              } catch (error) {
                toast.push(toUserMessage(error), "error");
              }
            }}
          >
            Remove
          </button>
        </div>
      </div>
    </div>
  );
}

function AccessDrawer({
  orgId,
  member,
  onClose,
}: {
  orgId: number;
  member: OrgMember;
  onClose: () => void;
}) {
  const projectsQuery = useProjects();
  const grantProject = useGrantProject(orgId);
  const revokeProject = useRevokeProject(orgId);
  const resetAccess = useResetOrgMemberAccess(orgId);
  const toast = useToast();

  const orgProjects = useMemo(
    () => (projectsQuery.data ?? [])
      .filter((project) => project.organizationId === orgId)
      .map((project) => ({ id: project.id, name: project.name })),
    [projectsQuery.data, orgId],
  );
  const roleByProject = useMemo(() => buildRoleByProject(member, orgProjects), [member, orgProjects]);

  const displayName = [member.first_name, member.last_name].filter(Boolean).join(" ") || member.username;
  const busy = grantProject.isPending || revokeProject.isPending || resetAccess.isPending;

  async function setRole(projectId: number, roleId: number | null) {
    try {
      if (roleId === null) {
        await revokeProject.mutateAsync({ userId: member.user_id, projectId });
        toast.push("Access revoked.", "success");
      } else {
        await grantProject.mutateAsync({ userId: member.user_id, projectId, roleId });
        toast.push("Access updated.", "success");
      }
    } catch (error) {
      toast.push(toUserMessage(error), "error");
    }
  }

  return (
    <div className="fixed inset-0 z-[80] flex justify-end">
      <button type="button" aria-label="Close" className="flex-1 bg-black/50" onClick={onClose} />
      <aside className="flex h-full w-full max-w-lg flex-col border-l border-night-500 bg-night-900 shadow-xl">
        <div className="flex items-center justify-between border-b border-night-500 px-5 py-4">
          <div>
            <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Project access</div>
            <div className="mt-1 text-sm font-medium text-white">{displayName}</div>
          </div>
          <button type="button" className="aist-icon-button" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-5 py-4">
          {member.membership_type === "full" ? (
            <p className="mb-4 rounded-xl border border-night-500/70 bg-night-800/70 p-3 text-xs text-slate-400">
              This is a full organization member — they can already see every project at their organization role.
              Changes below only narrow a single project (a lower role, or "No access") and never affect any other
              project; a project role can never exceed their organization role.
            </p>
          ) : (
            <div className="mb-4 flex items-center justify-between gap-3 rounded-xl border border-night-500/70 bg-night-800/70 p-3">
              <p className="text-xs text-slate-400">
                This member is restricted to the projects granted below — an empty list means no project access at all.
              </p>
              <button
                type="button"
                className="aist-icon-button shrink-0 whitespace-nowrap"
                disabled={resetAccess.isPending}
                onClick={async () => {
                  if (!window.confirm(`Reset ${displayName} to full organization access? This clears their per-project grants and denials.`)) return;
                  try {
                    await resetAccess.mutateAsync(member.user_id);
                    toast.push("Member reset to full organization access.", "success");
                  } catch (error) {
                    toast.push(toUserMessage(error), "error");
                  }
                }}
              >
                Reset to full access
              </button>
            </div>
          )}
          {projectsQuery.isLoading ? (
            <p className="text-xs text-slate-400">Loading projects...</p>
          ) : (
            <ProjectAccessEditor
              projects={orgProjects}
              roleByProject={roleByProject}
              disabled={busy}
              maxRoleId={member.membership_type === "full" ? member.role_id : null}
              onSetRole={setRole}
            />
          )}
        </div>
      </aside>
    </div>
  );
}
