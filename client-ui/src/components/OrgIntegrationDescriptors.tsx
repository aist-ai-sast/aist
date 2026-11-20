import type { Dispatch, ReactNode, SetStateAction } from "react";

import type { DastOnboardingBundle, OrgIntegrationPayload, VpnSecretPayload } from "../lib/mutations";
import { useOrgIntegrations, type OrgIntegration } from "../lib/queries";
import { ORG_INTEGRATION_TYPES, type IntegrationType } from "../lib/integrationTypes";
import SelectField from "./SelectField";
import TextInput from "./TextInput";
import SecretTextareaField from "./SecretTextareaField";
import PasswordField from "./PasswordField";
import BuildEnvFields, { buildEnvRow, isIncompleteBuildEnvRow, type BuildEnvRow } from "./BuildEnvFields";
import VpnCredentialFields from "./VpnCredentialFields";
import { TYPE_LABELS, SaveCancelButtons } from "./OrgIntegrationUI";

// ---------------------------------------------------------------------------
// Form state
// ---------------------------------------------------------------------------

export type OrgIntegrationFormState = {
  name: string;
  integration_type: IntegrationType;
  secret: string;
  config: Record<string, string | undefined>;
  vpn_secret: VpnSecretPayload;
  build_env: BuildEnvRow[];
  vpn_integration: number | null;
  is_active: boolean;
};

const EMPTY_ORG_FORM: OrgIntegrationFormState = {
  name: "",
  integration_type: "GITLAB",
  secret: "",
  config: {},
  vpn_secret: {},
  build_env: [],
  vpn_integration: null,
  is_active: true,
};

export function formStateFor(editing: OrgIntegration | null): OrgIntegrationFormState {
  if (!editing) return EMPTY_ORG_FORM;
  return {
    name: editing.name,
    integration_type: editing.integration_type,
    secret: "",
    config: Object.fromEntries(Object.entries(editing.config ?? {}).map(([k, v]) => [k, String(v)])),
    vpn_secret: {},
    build_env: (editing.build_env ?? []).map((variable) => buildEnvRow({ name: variable.name, value: "", stored: true })),
    vpn_integration: editing.vpn_integration ?? null,
    is_active: editing.is_active,
  };
}

/** Config with blank values dropped and the rest trimmed. */
export function trimmedConfig(config: Record<string, string | undefined>): Record<string, unknown> {
  const trimmed: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(config)) {
    if (v?.trim()) trimmed[k] = v.trim();
  }
  return trimmed;
}

export type IntegrationFieldsProps = {
  orgId: number;
  editing: OrgIntegration | null;
  form: OrgIntegrationFormState;
  setForm: Dispatch<SetStateAction<OrgIntegrationFormState>>;
};

export type IntegrationFormProps = IntegrationFieldsProps & { onDone: () => void };

// ---------------------------------------------------------------------------
// Form frame: type picker, name, the type's fields, Save / Cancel
// ---------------------------------------------------------------------------

export function IntegrationFormFrame({
  editing,
  form,
  setForm,
  onSave,
  onCancel,
  isPending,
  isComplete,
  children,
}: Pick<IntegrationFieldsProps, "editing" | "form" | "setForm"> & {
  onSave: () => void;
  onCancel: () => void;
  isPending: boolean;
  isComplete: boolean;
  children: ReactNode;
}) {
  return (
    <div className="rounded-2xl border border-night-500/80 bg-night-700/60 p-4 space-y-3">
      <div className="text-xs uppercase tracking-[0.2em] text-slate-400">
        {editing ? "Edit Integration" : "New Integration"}
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
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
        {children}
      </div>
      <SaveCancelButtons
        onSave={onSave}
        onCancel={onCancel}
        isPending={isPending}
        disabled={!form.name.trim() || !isComplete}
        label={editing ? "Save changes" : "Create"}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Field building blocks
// ---------------------------------------------------------------------------

function ConfigTextField({
  form,
  setForm,
  configKey,
  label,
  hint,
  placeholder,
  wide = true,
  mono = false,
}: IntegrationFieldsProps & {
  configKey: string;
  label: string;
  hint?: string;
  placeholder: string;
  wide?: boolean;
  mono?: boolean;
}) {
  return (
    <label className={wide ? "text-xs text-slate-400 sm:col-span-2" : "text-xs text-slate-400"}>
      {label}
      {hint && <>{" "}<span className="text-slate-500">{hint}</span></>}
      <TextInput
        className={mono ? "mt-1 font-mono" : "mt-1"}
        placeholder={placeholder}
        value={form.config[configKey] ?? ""}
        onChange={(e) => setForm((prev) => ({ ...prev, config: { ...prev.config, [configKey]: e.target.value } }))}
      />
    </label>
  );
}

function SecretTokenField({
  editing,
  form,
  setForm,
  label,
  optional = false,
}: IntegrationFieldsProps & { label: string; optional?: boolean }) {
  return (
    <label className="text-xs text-slate-400 sm:col-span-2">
      {label}
      {optional && <span className="text-slate-500"> (optional)</span>}
      {editing?.has_secret && (
        <span className="ml-1 text-slate-500">(leave blank to keep existing)</span>
      )}
      <PasswordField
        className="mt-1"
        placeholder={editing?.has_secret ? "••••••••••••••••" : ""}
        value={form.secret}
        onChange={(e) => setForm((prev) => ({ ...prev, secret: e.target.value }))}
      />
    </label>
  );
}

/** Routes the integration's traffic through one of the organization's active VPN integrations. */
function VpnRouteField({ orgId, form, setForm }: IntegrationFieldsProps) {
  const { data: orgIntegrations } = useOrgIntegrations(orgId);
  const vpnOptions = (orgIntegrations ?? []).filter((i) => i.integration_type === "VPN" && i.is_active);
  return (
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
  );
}

function DastConnectionFields(props: IntegrationFieldsProps) {
  const { form, setForm } = props;
  return (
    <>
      <ConfigTextField {...props} label="Gateway URL" configKey="gateway_url" placeholder="https://dast-gateway.internal" />
      <ConfigTextField {...props} label="Integrator ID" configKey="integrator_public_id" placeholder="pub_aist" wide={false} />
      <ConfigTextField {...props} label="Server fingerprint" configKey="server_fingerprint" placeholder="sha256:..." wide={false} mono />
      <label className="text-xs text-slate-400 sm:col-span-2">
        Public CA bundle <span className="text-slate-500">(blank uses system trust)</span>
        <SecretTextareaField
          className="mt-1"
          rows={4}
          placeholder="-----BEGIN CERTIFICATE-----"
          value={form.config.ca_bundle ?? ""}
          onChange={(e) => setForm((prev) => ({ ...prev, config: { ...prev.config, ca_bundle: e.target.value } }))}
        />
      </label>
    </>
  );
}

function ClaudeTokenHint() {
  return (
    <div className="text-xs text-slate-400 sm:col-span-2">
      Paste the OAuth token produced by{" "}
      <code className="rounded bg-night-700/60 px-1 py-0.5 text-[11px] text-slate-300">
        claude setup-token
      </code>{" "}
      into the access-token field below. Use the “Validate” button
      after saving to confirm the token against the Anthropic API.
    </div>
  );
}

// ---------------------------------------------------------------------------
// Payload contributions and completeness
// ---------------------------------------------------------------------------

/** What a type adds to the name, trimmed config and active flag of an integration save. */
export type IntegrationPayloadFields = Partial<Omit<OrgIntegrationPayload, "integration_type" | "name" | "is_active">>;

/** The VPN route and, when one was typed, the access token. */
function connectionPayload(form: OrgIntegrationFormState): IntegrationPayloadFields {
  const secret = form.secret.trim();
  return { vpn_integration: form.vpn_integration, ...(secret ? { secret } : {}) };
}

function vpnPayload(form: OrgIntegrationFormState): IntegrationPayloadFields {
  const vpnSecret: VpnSecretPayload = {};
  for (const [k, v] of Object.entries(form.vpn_secret)) {
    if (v?.trim()) vpnSecret[k as keyof VpnSecretPayload] = v.trim();
  }
  return Object.keys(vpnSecret).length > 0 ? { vpn_secret: vpnSecret } : {};
}

function buildEnvPayload(form: OrgIntegrationFormState): IntegrationPayloadFields {
  return {
    vpn_integration: form.vpn_integration,
    build_env: form.build_env.map((row) =>
      row.value ? { name: row.name.trim(), value: row.value } : { name: row.name.trim() },
    ),
  };
}

function claudePayload(form: OrgIntegrationFormState): IntegrationPayloadFields {
  const config = trimmedConfig(form.config);
  // The form has no auth-mode selector; Claude integrations use OAuth.
  return { ...connectionPayload(form), config: { ...config, auth_mode: config.auth_mode ?? "oauth" } };
}

/** DAST connection fields the backend only accepts as part of a signed onboarding bundle. */
const DAST_CONNECTION_FIELDS = [
  "gateway_url",
  "ca_bundle",
  "integrator_public_id",
  "server_fingerprint",
] as const;

/**
 * The onboarding bundle, or ``null`` when no integrator token was supplied. The stored token is
 * never readable, so an edit without one keeps the stored connection.
 */
function dastBundle(form: OrgIntegrationFormState): DastOnboardingBundle | null {
  if (!form.secret) return null;
  return {
    bundle_version: 1,
    gateway_url: form.config.gateway_url?.trim() ?? "",
    ca_bundle: form.config.ca_bundle?.trim() ?? "",
    contract_major: 2,
    integrator_public_id: form.config.integrator_public_id?.trim() ?? "",
    server_fingerprint: form.config.server_fingerprint?.trim() ?? "",
    token: form.secret,
  };
}

/** Connection fields reach the backend only inside a bundle, so changing them requires the token. */
function isDastComplete(form: OrgIntegrationFormState, editing: OrgIntegration | null): boolean {
  const connectionEdited =
    editing != null &&
    DAST_CONNECTION_FIELDS.some(
      (key) => (form.config[key]?.trim() ?? "") !== String(editing.config?.[key] ?? "").trim(),
    );
  return (
    !!form.config.gateway_url?.trim() &&
    !!form.config.integrator_public_id?.trim() &&
    !!form.config.server_fingerprint?.trim() &&
    (!!form.secret || (editing != null && !connectionEdited))
  );
}

const alwaysComplete = () => true;

// ---------------------------------------------------------------------------
// Per-type descriptors
// ---------------------------------------------------------------------------

export type IntegrationDescriptor<Contribution> = {
  /** The type's inputs, rendered in the form grid after Type and Name. */
  renderFields: (props: IntegrationFieldsProps) => ReactNode;
  /** What the type contributes to its save request. */
  payload: (form: OrgIntegrationFormState) => Contribution;
  /** Whether the type's inputs hold enough to save. */
  isComplete: (form: OrgIntegrationFormState, editing: OrgIntegration | null) => boolean;
};

/** DAST saves through its onboarding endpoint with a bundle; every other type through the integration endpoint. */
type PayloadContribution<T extends IntegrationType> = T extends "DAST"
  ? DastOnboardingBundle | null
  : IntegrationPayloadFields;

export const ORG_INTEGRATION_DESCRIPTORS: {
  [T in IntegrationType]: IntegrationDescriptor<PayloadContribution<T>>;
} = {
  GITLAB: {
    renderFields: (props) => (
      <>
        <ConfigTextField {...props} label="GitLab URL" configKey="base_url" placeholder="https://gitlab.com" />
        <SecretTokenField {...props} label="Access Token" />
        <VpnRouteField {...props} />
      </>
    ),
    payload: connectionPayload,
    isComplete: alwaysComplete,
  },
  GITHUB: {
    renderFields: (props) => (
      <>
        <ConfigTextField
          {...props}
          label="API URL"
          hint="(optional, for GHES)"
          configKey="base_api_url"
          placeholder="https://api.github.com"
        />
        <SecretTokenField {...props} label="Access Token" optional />
      </>
    ),
    payload: connectionPayload,
    isComplete: alwaysComplete,
  },
  GERRIT: {
    renderFields: (props) => (
      <>
        <ConfigTextField {...props} label="Gerrit URL" configKey="base_url" placeholder="https://gerrit.example.com" />
        <ConfigTextField {...props} label="HTTP username" configKey="username" placeholder="Gerrit HTTP account username" />
        <SecretTokenField {...props} label="Access Token" />
        <VpnRouteField {...props} />
      </>
    ),
    payload: connectionPayload,
    isComplete: alwaysComplete,
  },
  GITEA: {
    renderFields: (props) => (
      <>
        <ConfigTextField {...props} label="Gitea URL" configKey="base_url" placeholder="https://gitea.example.com" />
        <SecretTokenField {...props} label="Access Token" />
        <VpnRouteField {...props} />
      </>
    ),
    payload: connectionPayload,
    isComplete: alwaysComplete,
  },
  SLACK: {
    renderFields: (props) => (
      <>
        <ConfigTextField {...props} label="Default channel" configKey="default_channel" placeholder="#alerts or channel ID" />
        <SecretTokenField {...props} label="Bot Token" />
      </>
    ),
    payload: connectionPayload,
    isComplete: (form) => !!form.config.default_channel?.trim(),
  },
  EMAIL: {
    renderFields: (props) => (
      <ConfigTextField {...props} label="From address" configKey="from_email" placeholder="noreply@example.com" />
    ),
    payload: connectionPayload,
    isComplete: (form) => !!form.config.from_email?.trim(),
  },
  VPN: {
    renderFields: ({ editing, form, setForm }) => (
      <VpnCredentialFields
        vpnSecret={form.vpn_secret}
        status={editing?.vpn_secret}
        config={form.config}
        onChange={(key, value) =>
          setForm((prev) => ({ ...prev, vpn_secret: { ...prev.vpn_secret, [key]: value } }))
        }
        onConfigChange={(key, value) =>
          setForm((prev) => ({ ...prev, config: { ...prev.config, [key]: value } }))
        }
      />
    ),
    payload: vpnPayload,
    isComplete: alwaysComplete,
  },
  CLAUDE_CODE: {
    renderFields: (props) => (
      <>
        <ClaudeTokenHint />
        <SecretTokenField {...props} label="Access Token" />
      </>
    ),
    payload: claudePayload,
    isComplete: alwaysComplete,
  },
  DAST: {
    renderFields: (props) => (
      <>
        <DastConnectionFields {...props} />
        <SecretTokenField {...props} label="Integrator Token" />
        <VpnRouteField {...props} />
      </>
    ),
    payload: dastBundle,
    isComplete: isDastComplete,
  },
  BUILD_ENV: {
    renderFields: ({ form, setForm }) => (
      <BuildEnvFields rows={form.build_env} onChange={(rows) => setForm((prev) => ({ ...prev, build_env: rows }))} />
    ),
    payload: buildEnvPayload,
    isComplete: (form) => !form.build_env.some(isIncompleteBuildEnvRow),
  },
};
