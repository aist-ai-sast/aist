import { useEffect, useState, type ChangeEvent } from "react";

import { toUserMessage } from "../lib/api";
import {
  useCreateOrgIntegration,
  useDeleteOrgIntegration,
  useUpdateOrgIntegration,
  useValidateOrgIntegration,
  useCreateWorkItemProvider,
  useDeleteWorkItemProvider,
  useUpdateWorkItemProvider,
  useValidateWorkItemProvider,
  useSetProjectIntegrationOverride,
  useDeleteProjectIntegrationOverride,
  type OrgIntegrationPayload,
  type VpnSecretPayload,
  type WorkItemProviderPayload,
} from "../lib/mutations";
import PermissionGate from "../components/PermissionGate";
import {
  useManageableOrgs,
  useOrgIntegrations,
  useWorkItemProviders,
  useProjectIntegrationOverrides,
  useProjects,
  useValidationStatus,
  useWorkItemProviderValidationStatus,
  type OrgIntegration,
  type VpnSecretStatus,
  type WorkItemProviderSummary,
} from "../lib/queries";
import { useToast } from "../components/ToastProvider";
import SelectField from "../components/SelectField";
import TextInput from "../components/TextInput";
import SecretTextareaField from "../components/SecretTextareaField";
import { PROVIDER_ICON_PATHS } from "../lib/providerIcons";

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

type IntegrationType = "GITLAB" | "GITHUB" | "SLACK" | "EMAIL" | "VPN";

const ORG_INTEGRATION_TYPES: IntegrationType[] = ["GITLAB", "GITHUB", "SLACK", "EMAIL", "VPN"];

const TYPE_LABELS: Record<string, string> = {
  GITLAB: "GitLab",
  GITHUB: "GitHub",
  SLACK: "Slack",
  EMAIL: "Email",
  VPN: "VPN",
  JIRA: "Jira",
  YOUTRACK: "YouTrack",
  LINEAR: "Linear",
  AZURE_DEVOPS: "Azure DevOps",
  GENERIC: "Generic",
};

const TYPE_BADGE_CLASSES: Record<string, string> = {
  GITLAB: "border-orange-500/40 bg-orange-500/10 text-orange-300",
  GITHUB: "border-slate-400/40 bg-slate-400/10 text-slate-300",
  SLACK: "border-green-500/40 bg-green-500/10 text-green-300",
  EMAIL: "border-brand-500/40 bg-brand-500/10 text-brand-300",
  JIRA: "border-blue-500/40 bg-blue-500/10 text-blue-300",
  YOUTRACK: "border-purple-500/40 bg-purple-500/10 text-purple-300",
  LINEAR: "border-indigo-500/40 bg-indigo-500/10 text-indigo-300",
  AZURE_DEVOPS: "border-cyan-500/40 bg-cyan-500/10 text-cyan-300",
  GENERIC: "border-slate-400/30 bg-slate-400/10 text-slate-400",
  VPN: "border-slate-400/40 bg-slate-400/10 text-slate-300",
};


function ProviderIcon({ type }: { type: string }) {
  const d = PROVIDER_ICON_PATHS[type];
  if (!d) return null;
  return (
    <svg viewBox="0 0 24 24" className="h-3 w-3 shrink-0" aria-hidden="true">
      <path fill="currentColor" d={d} />
    </svg>
  );
}

function TypeBadge({ type }: { type: string }) {
  const cls = TYPE_BADGE_CLASSES[type] ?? TYPE_BADGE_CLASSES.GENERIC;
  return (
    <span className={`inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${cls}`}>
      <ProviderIcon type={type} />
      {TYPE_LABELS[type] ?? type}
    </span>
  );
}

function LockIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
      <path fill="currentColor" d="M18 8h-1V6a5 5 0 0 0-10 0v2H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V10a2 2 0 0 0-2-2Zm-6 9a2 2 0 1 1 0-4 2 2 0 0 1 0 4Zm3.1-9H8.9V6a3.1 3.1 0 0 1 6.2 0v2Z" />
    </svg>
  );
}

function PasswordField({
  value,
  onChange,
  placeholder,
  className,
}: {
  value: string;
  onChange: (e: ChangeEvent<HTMLInputElement>) => void;
  placeholder?: string;
  className?: string;
}) {
  const [show, setShow] = useState(false);
  return (
    <div className={`relative ${className ?? ""}`}>
      <TextInput
        variant="password"
        type={show ? "text" : "password"}
        className="pr-10"
        placeholder={placeholder}
        value={value}
        onChange={onChange}
      />
      <button
        type="button"
        className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-200 transition"
        onClick={() => setShow((s) => !s)}
        tabIndex={-1}
        aria-label={show ? "Hide" : "Show"}
      >
        {show ? (
          <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
            <path fill="currentColor" d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5C21.27 7.61 17 4.5 12 4.5ZM12 17a5 5 0 1 1 0-10 5 5 0 0 1 0 10Zm0-8a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
            <path fill="currentColor" d="M2 4.27 3.28 3 21 20.73 19.73 22l-3.08-3.08A11.8 11.8 0 0 1 12 19.5C7 19.5 2.73 16.39 1 12a11.8 11.8 0 0 1 4.38-5.62L2 4.27ZM12 7a5 5 0 0 1 4.78 3.54l-1.52-1.52A3 3 0 0 0 9.17 12.7L7.38 10.9A5 5 0 0 1 12 7Zm0-2.5c1.27 0 2.49.2 3.64.57L14.07 3.5A11.8 11.8 0 0 0 12 3.27C7 3.27 2.73 6.38 1 10.77a11.85 11.85 0 0 0 3.26 4.53L5.7 13.86A9.85 9.85 0 0 1 3.08 11c1.55-3.47 5.01-5.77 8.92-5.77Zm5.87 7.34a9.85 9.85 0 0 1-1.66 2.39l1.44 1.44A11.85 11.85 0 0 0 21 11C19.27 6.61 15 3.5 10 3.5c-.5 0-1 .03-1.48.09l1.84 1.84C10.23 5.43 10.6 5.4 11 5.4c3.91 0 7.37 2.3 8.92 5.77l-.05.17Z" />
          </svg>
        )}
      </button>
    </div>
  );
}

function SectionCard({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="aist-card border-night-500/80 p-0">
      <div className="border-b border-night-500/70 px-5 py-4">
        <div className="text-xs uppercase tracking-[0.2em] text-slate-300">{title}</div>
        {description && <p className="mt-0.5 text-xs text-slate-500">{description}</p>}
      </div>
      <div className="px-5 py-4 space-y-3">{children}</div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Generic resource row
// ---------------------------------------------------------------------------

function ResourceRow({
  typeKey,
  name,
  hasSecret,
  isActive,
  isDefault,
  vpnName,
  onEdit,
  onDelete,
  onValidate,
  isPendingDelete,
  isPendingValidate,
}: {
  typeKey: string;
  name: string;
  hasSecret: boolean;
  isActive: boolean;
  isDefault?: boolean;
  vpnName?: string;
  onEdit: () => void;
  onDelete: () => void;
  onValidate: () => void;
  isPendingDelete: boolean;
  isPendingValidate: boolean;
}) {
  return (
    <div className="flex items-center gap-3 rounded-2xl border border-night-500/80 bg-night-800/75 px-4 py-3">
      <TypeBadge type={typeKey} />
      <span className="min-w-0 flex-1 truncate text-sm font-medium text-slate-100">{name}</span>
      <div className="flex shrink-0 items-center gap-1.5 text-[11px]">
        {vpnName && (
          <span className="flex items-center gap-1 rounded-full border border-slate-500/30 bg-slate-500/10 px-2 py-0.5 text-[10px] text-slate-400">
            <svg viewBox="0 0 24 24" className="h-2.5 w-2.5 shrink-0" aria-hidden="true">
              <path fill="currentColor" d="M12 1 3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4Z" />
            </svg>
            via VPN · {vpnName}
          </span>
        )}
        {isDefault && (
          <span className="rounded-full border border-brand-500/40 bg-brand-500/10 px-2 py-0.5 text-[10px] text-brand-300">
            default
          </span>
        )}
        {hasSecret && (
          <span className="text-slate-400" title="Has stored credential">
            <LockIcon />
          </span>
        )}
        {!isActive && (
          <span className="rounded-full border border-slate-500/40 bg-slate-500/10 px-2 py-0.5 text-slate-400">
            inactive
          </span>
        )}
      </div>
      <div className="flex shrink-0 gap-1">
        <button
          className="aist-icon-button border-night-400/60 bg-night-700/60 text-slate-300 text-[11px] px-2.5 py-1.5"
          disabled={isPendingValidate}
          onClick={onValidate}
          title="Validate credentials"
        >
          {isPendingValidate ? (
            <>
              <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 animate-spin" aria-hidden="true">
                <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" opacity="0.25" />
                <path fill="currentColor" opacity="0.75" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4Z" />
              </svg>
              Validating…
            </>
          ) : (
            <>
              <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
                <path fill="currentColor" d="m9 12 2 2 4-4 1.4 1.4-5.4 5.4-3.4-3.4L9 12ZM12 2 4 6v6c0 5.5 3.8 9.9 8 11 4.2-1.1 8-5.5 8-11V6l-8-4Z" />
              </svg>
              Validate
            </>
          )}
        </button>
        <button
          className="aist-icon-button border-night-400/60 bg-night-700/60 text-slate-300 text-[11px] px-2.5 py-1.5"
          onClick={onEdit}
          title="Edit"
        >
          <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
            <path fill="currentColor" d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25ZM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83Z" />
          </svg>
          Edit
        </button>
        <button
          className="inline-flex items-center gap-1.5 rounded-xl border border-danger-500/50 bg-danger-500/10 px-2.5 py-1.5 text-[11px] font-semibold text-danger-400 transition hover:bg-danger-500/20 disabled:opacity-60"
          disabled={isPendingDelete}
          onClick={onDelete}
          title="Delete"
        >
          <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
            <path fill="currentColor" d="M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12ZM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4Z" />
          </svg>
          Delete
        </button>
      </div>
    </div>
  );
}

function AddButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      className="aist-icon-button border-brand-500/50 bg-brand-500/15 text-brand-100 hover:border-brand-400/70 hover:bg-brand-500/25"
      onClick={onClick}
    >
      <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
        <path fill="currentColor" d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2Z" />
      </svg>
      Add
    </button>
  );
}

function SaveCancelButtons({
  onSave,
  onCancel,
  isPending,
  disabled,
  label,
}: {
  onSave: () => void;
  onCancel: () => void;
  isPending: boolean;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <div className="flex gap-2">
      <button
        className="aist-icon-button border-brand-500/50 bg-brand-500/15 text-brand-100 hover:border-brand-400/70 hover:bg-brand-500/25"
        disabled={isPending || disabled}
        onClick={onSave}
      >
        <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
          <path fill="currentColor" d="M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z" />
        </svg>
        {label ?? "Save"}
      </button>
      <button
        className="aist-icon-button border-night-400/80 bg-night-800/80 text-slate-200"
        disabled={isPending}
        onClick={onCancel}
      >
        Cancel
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Org Integration form
// ---------------------------------------------------------------------------

type OrgIntegrationFormState = {
  name: string;
  integration_type: IntegrationType;
  secret: string;
  config: Record<string, string | undefined>;
  vpn_secret: VpnSecretPayload;
  vpn_integration: number | null;
  is_active: boolean;
};

const EMPTY_ORG_FORM: OrgIntegrationFormState = {
  name: "",
  integration_type: "GITLAB",
  secret: "",
  config: {},
  vpn_secret: {},
  vpn_integration: null,
  is_active: true,
};

function OrgIntegrationConfigFields({
  type,
  config,
  onChange,
}: {
  type: IntegrationType;
  config: Record<string, string | undefined>;
  onChange: (key: string, value: string) => void;
}) {
  if (type === "GITLAB") {
    return (
      <label className="text-xs text-slate-400 sm:col-span-2">
        GitLab URL
        <TextInput
          className="mt-1"
          placeholder="https://gitlab.com"
          value={config.base_url ?? ""}
          onChange={(e) => onChange("base_url", e.target.value)}
        />
      </label>
    );
  }
  if (type === "GITHUB") {
    return (
      <label className="text-xs text-slate-400 sm:col-span-2">
        API URL <span className="text-slate-500">(optional, for GHES)</span>
        <TextInput
          className="mt-1"
          placeholder="https://api.github.com"
          value={config.base_api_url ?? ""}
          onChange={(e) => onChange("base_api_url", e.target.value)}
        />
      </label>
    );
  }
  if (type === "SLACK") {
    return (
      <label className="text-xs text-slate-400 sm:col-span-2">
        Default channel
        <TextInput
          className="mt-1"
          placeholder="#alerts or channel ID"
          value={config.default_channel ?? ""}
          onChange={(e) => onChange("default_channel", e.target.value)}
        />
      </label>
    );
  }
  if (type === "EMAIL") {
    return (
      <label className="text-xs text-slate-400 sm:col-span-2">
        From address
        <TextInput
          className="mt-1"
          placeholder="noreply@example.com"
          value={config.from_email ?? ""}
          onChange={(e) => onChange("from_email", e.target.value)}
        />
      </label>
    );
  }
  return null;
}

// ---------------------------------------------------------------------------
// VPN-specific credential fields
// ---------------------------------------------------------------------------

function SecretFileUpload({
  accept,
  onLoad,
  label,
}: {
  accept: string;
  onLoad: (text: string) => void;
  label: string;
}) {
  return (
    <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-night-400/50 bg-night-600 px-3 py-1.5 text-xs text-slate-300 transition hover:bg-night-500">
      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 shrink-0" aria-hidden="true">
        <path fill="currentColor" d="M9 16h6v-6h4l-7-7-7 7h4v6zm-4 2h14v2H5v-2z" />
      </svg>
      {label}
      <input
        type="file"
        accept={accept}
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (!file) return;
          const reader = new FileReader();
          reader.onload = (ev) => onLoad((ev.target?.result as string) ?? "");
          reader.readAsText(file);
          // Reset so same file can be re-selected if needed
          e.target.value = "";
        }}
      />
    </label>
  );
}

function UploadedBadge({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-green-500/30 bg-green-500/10 px-2 py-0.5 text-[11px] text-green-300">
      <svg viewBox="0 0 24 24" className="h-3 w-3" aria-hidden="true">
        <path fill="currentColor" d="M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z" />
      </svg>
      {label}
    </span>
  );
}

function VpnSecretField({
  label,
  fieldKey,
  value,
  uploaded,
  onChange,
  accept,
  uploadLabel,
  rows = 3,
  hint,
}: {
  label: string;
  fieldKey: keyof VpnSecretPayload;
  value: string;
  uploaded?: boolean;
  onChange: (key: keyof VpnSecretPayload, value: string) => void;
  accept: string;
  uploadLabel: string;
  rows?: number;
  hint?: string;
}) {
  return (
    <label className="text-xs text-slate-400 sm:col-span-2">
      <div className="mb-1 flex flex-wrap items-center gap-2">
        <span>{label}</span>
        {uploaded && <UploadedBadge label="Uploaded" />}
        {uploaded && (
          <span className="text-slate-500">(upload or paste to replace)</span>
        )}
        <SecretFileUpload
          accept={accept}
          label={uploadLabel}
          onLoad={(text) => onChange(fieldKey, text)}
        />
      </div>
      {hint && <p className="mb-1 text-[11px] text-slate-500">{hint}</p>}
      <SecretTextareaField
        rows={rows}
        value={value}
        onChange={(e) => onChange(fieldKey, e.target.value)}
        placeholder={uploaded ? "••••••••••••••••" : ""}
      />
    </label>
  );
}

function OrgIntegrationVPNFields({
  vpnSecret,
  status,
  config,
  onChange,
  onConfigChange,
}: {
  vpnSecret: VpnSecretPayload;
  status: VpnSecretStatus | undefined;
  config: Record<string, string | undefined>;
  onChange: (key: keyof VpnSecretPayload, value: string) => void;
  onConfigChange: (key: string, value: string) => void;
}) {
  return (
    <>
      <div className="text-[11px] uppercase tracking-[0.15em] text-slate-500 sm:col-span-2">
        VPN Configuration
      </div>
      <VpnSecretField
        label=".ovpn Config"
        fieldKey="ovpn_content"
        value={vpnSecret.ovpn_content ?? ""}
        uploaded={status?.has_ovpn_content && !vpnSecret.ovpn_content}
        onChange={onChange}
        accept=".ovpn,.conf,.txt"
        uploadLabel="Upload .ovpn"
        rows={6}
        hint="Full .ovpn file with inline certificates, or base config (attach certs below)."
      />
      <VpnSecretField
        label="CA Certificate"
        fieldKey="ca_cert"
        value={vpnSecret.ca_cert ?? ""}
        onChange={onChange}
        accept=".pem,.crt,.cer,.ca"
        uploadLabel="Upload CA cert"
        hint="Optional — if not embedded in .ovpn."
      />
      <VpnSecretField
        label="Client Certificate"
        fieldKey="client_cert"
        value={vpnSecret.client_cert ?? ""}
        uploaded={status?.has_client_cert && !vpnSecret.client_cert}
        onChange={onChange}
        accept=".pem,.crt,.cer"
        uploadLabel="Upload cert"
        hint="Optional — if not embedded in .ovpn."
      />
      <VpnSecretField
        label="Client Key"
        fieldKey="client_key"
        value={vpnSecret.client_key ?? ""}
        uploaded={status?.has_client_key && !vpnSecret.client_key}
        onChange={onChange}
        accept=".pem,.key"
        uploadLabel="Upload key"
        hint="Optional — if not embedded in .ovpn."
      />
      <VpnSecretField
        label="TLS Auth / Static Key"
        fieldKey="tls_auth_key"
        value={vpnSecret.tls_auth_key ?? ""}
        onChange={onChange}
        accept=".key,.txt"
        uploadLabel="Upload TLS key"
        hint="Optional — ta.key or static.key for tls-auth / tls-crypt."
      />
      <div className="text-[11px] uppercase tracking-[0.15em] text-slate-500 sm:col-span-2">
        Credentials
      </div>
      <label className="text-xs text-slate-400">
        Username
        {status?.has_username && !vpnSecret.vpn_username && (
          <span className="ml-2">
            <UploadedBadge label="Saved" />
          </span>
        )}
        <TextInput
          className="mt-1"
          placeholder={status?.has_username && !vpnSecret.vpn_username ? "••••••••" : ""}
          value={vpnSecret.vpn_username ?? ""}
          onChange={(e) => onChange("vpn_username", e.target.value)}
          autoComplete="off"
        />
      </label>
      <label className="text-xs text-slate-400">
        Password
        <PasswordField
          className="mt-1"
          placeholder={status?.has_username && !vpnSecret.vpn_password ? "••••••••" : ""}
          value={vpnSecret.vpn_password ?? ""}
          onChange={(e) => onChange("vpn_password", e.target.value)}
        />
      </label>
      <div className="text-[11px] uppercase tracking-[0.15em] text-slate-500 sm:col-span-2">
        Connectivity Check
      </div>
      <label className="text-xs text-slate-400 sm:col-span-2">
        Ping target IP
        <span className="ml-1 text-slate-500">(optional)</span>
        <TextInput
          className="mt-1"
          placeholder="10.0.0.1"
          value={config.ping_target ?? ""}
          onChange={(e) => onConfigChange("ping_target", e.target.value)}
        />
        <p className="mt-1 text-[11px] text-slate-500">
          Used by the Validate button to check VPN connectivity after tunnel is established.
        </p>
      </label>
    </>
  );
}

function OrgIntegrationForm({
  orgId,
  editing,
  onDone,
}: {
  orgId: number;
  editing: OrgIntegration | null;
  onDone: () => void;
}) {
  const toast = useToast();
  const createIntegration = useCreateOrgIntegration(orgId);
  const updateIntegration = useUpdateOrgIntegration(orgId);
  const { data: orgIntegrations } = useOrgIntegrations(orgId);
  const vpnOptions = (orgIntegrations ?? []).filter((i) => i.integration_type === "VPN" && i.is_active);

  const [form, setForm] = useState<OrgIntegrationFormState>(() =>
    editing
      ? {
          name: editing.name,
          integration_type: editing.integration_type as IntegrationType,
          secret: "",
          config: Object.fromEntries(Object.entries(editing.config ?? {}).map(([k, v]) => [k, String(v)])),
          vpn_secret: {},
          vpn_integration: editing.vpn_integration ?? null,
          is_active: editing.is_active,
        }
      : EMPTY_ORG_FORM,
  );

  useEffect(() => {
    setForm(
      editing
        ? {
            name: editing.name,
            integration_type: editing.integration_type as IntegrationType,
            secret: "",
            config: Object.fromEntries(Object.entries(editing.config ?? {}).map(([k, v]) => [k, String(v)])),
            vpn_secret: {},
            vpn_integration: editing.vpn_integration ?? null,
            is_active: editing.is_active,
          }
        : EMPTY_ORG_FORM,
    );
  }, [editing]);

  async function handleSave() {
    const config: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(form.config)) {
      if (v?.trim()) config[k] = v.trim();
    }
    const payload: OrgIntegrationPayload = {
      integration_type: form.integration_type,
      name: form.name.trim(),
      config,
      is_active: form.is_active,
      vpn_integration: form.integration_type !== "VPN" ? form.vpn_integration : undefined,
    };
    if (form.integration_type === "VPN") {
      const vpnPayload: VpnSecretPayload = {};
      for (const [k, v] of Object.entries(form.vpn_secret)) {
        if ((v as string)?.trim()) {
          vpnPayload[k as keyof VpnSecretPayload] = (v as string).trim();
        }
      }
      if (Object.keys(vpnPayload).length > 0) payload.vpn_secret = vpnPayload;
    } else {
      if (form.secret.trim()) payload.secret = form.secret.trim();
    }
    try {
      if (editing) {
        await updateIntegration.mutateAsync({ integrationId: editing.id, payload });
        toast.push("Integration updated.", "success");
      } else {
        await createIntegration.mutateAsync(payload);
        toast.push("Integration created.", "success");
      }
      onDone();
    } catch (error) {
      toast.push(toUserMessage(error), "error");
    }
  }

  const isPending = createIntegration.isPending || updateIntegration.isPending;

  return (
    <div className="rounded-2xl border border-night-500/80 bg-night-700/60 p-4 space-y-3">
      <div className="text-xs uppercase tracking-[0.2em] text-slate-400">
        {editing ? "Edit Integration" : "New Integration"}
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="sm:col-span-2">
          <SelectField
            label="Type"
            value={form.integration_type}
            onChange={(value) =>
              setForm((prev) => ({ ...prev, integration_type: value as IntegrationType, config: {} }))
            }
            options={ORG_INTEGRATION_TYPES.map((t) => ({ value: t, label: TYPE_LABELS[t] }))}
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
        {form.integration_type === "VPN" ? (
          <OrgIntegrationVPNFields
            vpnSecret={form.vpn_secret}
            status={(editing as (OrgIntegration & { vpn_secret?: VpnSecretStatus }) | null)?.vpn_secret}
            config={form.config}
            onChange={(key, value) =>
              setForm((prev) => ({ ...prev, vpn_secret: { ...prev.vpn_secret, [key]: value } }))
            }
            onConfigChange={(key, value) =>
              setForm((prev) => ({ ...prev, config: { ...prev.config, [key]: value } }))
            }
          />
        ) : (
          <>
            <OrgIntegrationConfigFields
              type={form.integration_type}
              config={form.config}
              onChange={(key, value) => setForm((prev) => ({ ...prev, config: { ...prev.config, [key]: value } }))}
            />
            {form.integration_type !== "EMAIL" && (
              <label className="text-xs text-slate-400 sm:col-span-2">
                {form.integration_type === "SLACK" ? "Bot Token" : "Access Token"}
                {form.integration_type === "GITHUB" && <span className="text-slate-500"> (optional)</span>}
                {editing?.has_secret && <span className="ml-1 text-slate-500">(leave blank to keep existing)</span>}
                <PasswordField
                  className="mt-1"
                  placeholder={editing?.has_secret ? "••••••••••••••••" : ""}
                  value={form.secret}
                  onChange={(e) => setForm((prev) => ({ ...prev, secret: e.target.value }))}
                />
              </label>
            )}
            {form.integration_type === "GITLAB" && (
              <div className="sm:col-span-2">
                <SelectField
                  label="VPN Integration"
                  value={form.vpn_integration !== null ? String(form.vpn_integration) : ""}
                  onChange={(value) =>
                    setForm((prev) => ({ ...prev, vpn_integration: value ? Number(value) : null }))
                  }
                  placeholder="None (direct connection)"
                  clearable
                  clearLabel="None (direct connection)"
                  options={vpnOptions.map((i) => ({ value: String(i.id), label: i.name }))}
                />
                {vpnOptions.length === 0 && (
                  <p className="mt-1 text-[11px] text-slate-500">
                    No active VPN integrations configured for this organization.
                  </p>
                )}
              </div>
            )}
          </>
        )}
        <label className="flex items-center gap-2 text-xs text-slate-400 sm:col-span-2">
          <input
            type="checkbox"
            className="accent-brand-500"
            checked={form.is_active}
            onChange={(e) => setForm((prev) => ({ ...prev, is_active: e.target.checked }))}
          />
          Active
        </label>
      </div>
      <SaveCancelButtons
        onSave={handleSave}
        onCancel={onDone}
        isPending={isPending}
        disabled={
          !form.name.trim() ||
          (form.integration_type === "EMAIL" && !form.config.from_email?.trim()) ||
          (form.integration_type === "SLACK" && !form.config.default_channel?.trim())
        }
        label={editing ? "Save changes" : "Create"}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Org Integrations section
// ---------------------------------------------------------------------------

function OrgIntegrationsSection({ orgId }: { orgId: number }) {
  const toast = useToast();
  const integrationsQuery = useOrgIntegrations(orgId);
  const deleteIntegration = useDeleteOrgIntegration(orgId);
  const validateIntegration = useValidateOrgIntegration();
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [validatingState, setValidatingState] = useState<{ integrationId: number; taskId: string } | null>(null);

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
    if (s === "SUCCESS" || s === "FAILURE") {
      const data = validationStatus.data!;
      toast.push(
        data.valid
          ? "Credentials are valid."
          : `Validation failed: ${data.detail || "Check integration configuration."}`,
        data.valid ? "success" : "error",
      );
      setValidatingState(null);
    }
  }, [validationStatus.data?.state]);

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
    if (!confirm(`Delete integration "${integration.name}"?`)) return;
    try {
      await deleteIntegration.mutateAsync(integration.id);
      toast.push("Integration deleted.", "success");
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

  function handleDone() {
    setShowForm(false);
    setEditingId(null);
  }

  return (
    <SectionCard
      title="Org Integrations"
      description="GitLab, GitHub, Slack, and Email accounts and credentials shared across all projects. The first active integration per type is used by default."
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
            onDelete={() => handleDelete(integration)}
            onValidate={() => handleValidate(integration)}
            isPendingDelete={deleteIntegration.isPending}
            isPendingValidate={
              (validateIntegration.isPending && validateIntegration.variables === integration.id) ||
              validatingState?.integrationId === integration.id
            }
          />
        ),
      )}
      {showForm && !editingId ? (
        <OrgIntegrationForm orgId={orgId} editing={null} onDone={handleDone} />
      ) : !editingId ? (
        <AddButton onClick={() => setShowForm(true)} />
      ) : null}
    </SectionCard>
  );
}

// ---------------------------------------------------------------------------
// Work Item Provider form
// ---------------------------------------------------------------------------

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
      <div className="grid gap-3 sm:grid-cols-2">
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
                — instance root only, e.g. <code className="text-slate-300">https://company.atlassian.net</code>
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

// ---------------------------------------------------------------------------
// Work Item Providers section
// ---------------------------------------------------------------------------

function WorkItemProvidersSection({ orgId }: { orgId: number }) {
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
          />
        ),
      )}
      {showForm && !editingId ? (
        <WorkItemProviderForm orgId={orgId} editing={null} onDone={handleDone} />
      ) : !editingId ? (
        <AddButton onClick={() => setShowForm(true)} />
      ) : null}
    </SectionCard>
  );
}

// ---------------------------------------------------------------------------
// Per-project integration overrides section
// ---------------------------------------------------------------------------

function ProjectOverridesSection({ orgId }: { orgId: number }) {
  const toast = useToast();
  const projectsQuery = useProjects();
  const integrationsQuery = useOrgIntegrations(orgId);
  const [selectedProjectId, setSelectedProjectId] = useState<number | null>(null);

  const overridesQuery = useProjectIntegrationOverrides(selectedProjectId ?? undefined);
  const setOverride = useSetProjectIntegrationOverride(selectedProjectId ?? 0);
  const deleteOverride = useDeleteProjectIntegrationOverride(selectedProjectId ?? 0);

  const projects = (projectsQuery.data ?? []).filter((project) => project.organizationId === orgId);
  const integrations = integrationsQuery.data ?? [];
  const overrides = overridesQuery.data ?? [];

  function getOverride(type: string) {
    return overrides.find((o) => o.integration_type === type) ?? null;
  }

  function getIntegrationsOfType(type: string) {
    return integrations.filter((i) => i.integration_type === type);
  }

  async function handleSetOverride(type: string, integrationId: number | null) {
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

  async function handleToggleVpnDisabled(disable: boolean) {
    if (!selectedProjectId) return;
    try {
      if (disable) {
        await setOverride.mutateAsync({ integrationType: "VPN", isDisabled: true });
        toast.push("VPN disabled for this project.", "success");
      } else {
        await deleteOverride.mutateAsync("VPN");
        toast.push("VPN override cleared — org default will be used.", "success");
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
          {ORG_INTEGRATION_TYPES.map((type) => {
            const typeIntegrations = getIntegrationsOfType(type);
            const override = getOverride(type);

            if (typeIntegrations.length === 0) return null;

            if (type === "VPN") {
              const isDisabled = override?.is_disabled ?? false;
              return (
                <div key={type} className="flex items-center gap-3 rounded-xl border border-night-500/60 bg-night-800/50 px-3 py-2.5">
                  <TypeBadge type={type} />
                  <div className="flex-1 text-sm text-slate-300">VPN</div>
                  <div className="flex items-center gap-2">
                    {isDisabled && (
                      <span className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] text-amber-400">
                        disabled for this project
                      </span>
                    )}
                    <label className="flex cursor-pointer items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 transition">
                      <input
                        type="checkbox"
                        checked={isDisabled}
                        onChange={(e) => handleToggleVpnDisabled(e.target.checked)}
                        className="accent-amber-500"
                      />
                      Disable VPN
                    </label>
                  </div>
                </div>
              );
            }

            const currentValue = override?.org_integration ?? null;

            return (
              <div key={type} className="flex items-center gap-3 rounded-xl border border-night-500/60 bg-night-800/50 px-3 py-2.5">
                <TypeBadge type={type} />
                <div className="flex-1">
                  <SelectField
                    label={TYPE_LABELS[type]}
                    hideLabel
                    value={currentValue !== null ? String(currentValue) : ""}
                    onChange={(value) => handleSetOverride(type, Number(value))}
                    placeholder="org default"
                    options={typeIntegrations.map((i) => ({ value: String(i.id), label: i.name }))}
                  />
                </div>
                {override && (
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

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

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

  return (
    <PermissionGate
      action="manage_access"
      fallback={(
        <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-400">
          You need Maintainer or Owner role to manage integrations.
        </div>
      )}
    >
      {orgs.length === 0 ? (
        <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-400">
          No organization found. Contact your administrator.
        </div>
      ) : (
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
            </div>
          ))}
        </div>
      )}
    </PermissionGate>
  );
}
