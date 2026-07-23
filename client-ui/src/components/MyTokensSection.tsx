import { useEffect, useState, type ReactNode } from "react";

import { toUserMessage } from "../lib/api";
import { useAccountProfile } from "../lib/account";
import {
  type ApiTokenScope,
  type CreatedApiToken,
  useCreateToken,
  useDeleteToken,
  useMyTokens,
} from "../lib/apiTokens";
import SelectField from "./SelectField";
import TextInput from "./TextInput";
import { useToast } from "./ToastProvider";

const SCOPE_LABEL: Record<ApiTokenScope, string> = {
  read_only: "Read only",
  read_write: "Read / write",
};

function SectionCard({ title, icon, children }: { title: string; icon?: ReactNode; children: ReactNode }) {
  return (
    <section className="aist-card flex h-full flex-col border-night-500/80 p-0">
      <div className="border-b border-night-500/70 px-5 py-4">
        <div className="inline-flex items-center gap-2 text-xs uppercase tracking-[0.2em] text-slate-300">
          {icon ? <span className="text-brand-200">{icon}</span> : null}
          <span>{title}</span>
        </div>
      </div>
      <div className="flex-1 px-5 py-4">{children}</div>
    </section>
  );
}

export default function MyTokensSection() {
  const tokens = useMyTokens();
  const profile = useAccountProfile();
  const createToken = useCreateToken();
  const deleteToken = useDeleteToken();
  const toast = useToast();

  const [name, setName] = useState("");
  const [scope, setScope] = useState<ApiTokenScope>("read_only");
  const [organizationId, setOrganizationId] = useState<number | null>(null);
  const [created, setCreated] = useState<CreatedApiToken | null>(null);
  const [revealed, setRevealed] = useState(false);
  const organizations = profile.data?.organization_memberships ?? [];
  const selectedMembership = organizations.find(
    (organization) => organization.organization_id === organizationId,
  );
  const canCreateWriteToken = Boolean(profile.data?.is_superuser || selectedMembership?.can_write_findings);
  const scopeOptions = [
    { value: "read_only", label: "Read only" },
    {
      value: "read_write",
      label: "Read and write",
      disabled: !canCreateWriteToken,
    },
  ];

  // If write access is (or becomes) unavailable, never leave "read_write" selected —
  // the backend would reject it anyway (aist.queries.user_has_write_capability), this
  // just keeps the control itself from showing a choice the user can't act on.
  useEffect(() => {
    if (scope === "read_write" && !canCreateWriteToken) {
      setScope("read_only");
    }
  }, [scope, canCreateWriteToken]);

  useEffect(() => {
    if (organizationId === null && organizations.length) {
      setOrganizationId(organizations[0].organization_id);
    }
  }, [organizationId, organizations]);

  async function handleCreate() {
    if (!name.trim()) {
      toast.push("Give the token a name.", "error");
      return;
    }
    if (organizationId === null) {
      toast.push("Choose an organization for the token.", "error");
      return;
    }
    try {
      const token = await createToken.mutateAsync({ name: name.trim(), scope, organization_id: organizationId });
      setCreated(token);
      setName("");
      setScope("read_only");
    } catch (error) {
      toast.push(toUserMessage(error), "error");
    }
  }

  return (
    <SectionCard
      title="API Tokens"
      icon={(
        <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
          <path fill="currentColor" d="M14 2a6 6 0 0 0-5.7 8L2 16.3V22h5.7l6.3-6.3A6 6 0 1 0 14 2Zm2.5 6.5a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3Z" />
        </svg>
      )}
    >
      <div className="space-y-4 text-sm text-slate-200">
        <p className="text-xs text-slate-400">
          Personal tokens authenticate to the AIST API only and are restricted to one organization.
        </p>

        {created ? (
          <div className="rounded-2xl border border-brand-500/40 bg-brand-500/10 p-4">
            <div className="text-[11px] uppercase tracking-[0.2em] text-brand-100">Copy your token now</div>
            <p className="mt-1 text-xs text-slate-300">This secret is shown once and cannot be retrieved again.</p>
            <div className="mt-2 flex items-center gap-2">
              <code className="flex-1 overflow-x-auto rounded-lg border border-night-500 bg-night-800 px-3 py-2 text-xs text-brand-100">
                {revealed ? created.token : "•".repeat(Math.min(created.token.length, 40))}
              </code>
              <button
                type="button"
                className="aist-icon-button shrink-0"
                aria-pressed={revealed}
                onClick={() => setRevealed((value) => !value)}
              >
                {revealed ? "Hide" : "Show"}
              </button>
              <button
                type="button"
                className="aist-icon-button shrink-0"
                onClick={async () => {
                  await navigator.clipboard.writeText(created.token);
                  toast.push("Token copied.", "success");
                }}
              >
                Copy
              </button>
            </div>
            <button
              type="button"
              className="mt-3 text-[11px] text-slate-400 hover:text-slate-200"
              onClick={() => { setCreated(null); setRevealed(false); }}
            >
              Done
            </button>
          </div>
        ) : null}

        <div className="grid gap-3 sm:grid-cols-[1fr_12rem_12rem_auto] sm:items-end">
          <label className="text-xs text-slate-400">
            Name
            <TextInput
              variant="password"
              className="mt-1"
              placeholder="e.g. CI pipeline"
              value={name}
              disabled={createToken.isPending}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <SelectField
            label="Organization"
            value={organizationId === null ? "" : String(organizationId)}
            onChange={(value) => setOrganizationId(Number(value))}
            options={organizations.map((organization) => ({
              value: String(organization.organization_id),
              label: organization.organization_name,
            }))}
            disabled={createToken.isPending || organizations.length === 0}
          />
          <SelectField
            label="Scope"
            value={scope}
            onChange={(value) => setScope(value as ApiTokenScope)}
            options={scopeOptions}
            disabled={createToken.isPending}
          />
          <button className="aist-icon-button" disabled={createToken.isPending} onClick={handleCreate}>
            <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
              <path fill="currentColor" d="M11 11V5h2v6h6v2h-6v6h-2v-6H5v-2z" />
            </svg>
            Create token
          </button>
        </div>
        {!canCreateWriteToken ? (
          <p className="text-[11px] text-slate-400">
            You have read-only access in this organization, so only read-only tokens are available.
          </p>
        ) : null}

        <div className="border-t border-night-500 pt-4">
          {tokens.isLoading ? (
            <p className="text-xs text-slate-400">Loading tokens...</p>
          ) : tokens.data && tokens.data.length ? (
            <div className="space-y-2">
              {tokens.data.map((token) => (
                <div
                  key={token.id}
                  className="flex items-center justify-between gap-3 rounded-2xl border border-night-500/80 bg-night-800/75 px-3.5 py-3"
                >
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-slate-100">{token.name}</div>
                    <div className="mt-0.5 text-[11px] text-slate-400">
                      {token.organization_name} · {SCOPE_LABEL[token.scope]} · ····{token.last4}
                      {token.expires_at ? ` · expires ${new Date(token.expires_at).toLocaleDateString()}` : ""}
                      {token.last_used_at ? ` · last used ${new Date(token.last_used_at).toLocaleDateString()}` : " · never used"}
                    </div>
                  </div>
                  <button
                    type="button"
                    className="inline-flex shrink-0 items-center gap-2 rounded-xl border border-danger-500/70 bg-danger-500/10 px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.09em] text-danger-500 transition hover:bg-danger-500/20 disabled:opacity-60"
                    disabled={deleteToken.isPending}
                    onClick={async () => {
                      try {
                        await deleteToken.mutateAsync(token.id);
                        toast.push("Token revoked.", "success");
                      } catch (error) {
                        toast.push(toUserMessage(error), "error");
                      }
                    }}
                  >
                    Revoke
                  </button>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-slate-400">You have no API tokens yet.</p>
          )}
        </div>
      </div>
    </SectionCard>
  );
}
