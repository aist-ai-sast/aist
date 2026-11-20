import type { VpnSecretPayload } from "../lib/mutations";
import type { VpnSecretStatus } from "../lib/queries";
import TextInput from "./TextInput";
import SecretTextareaField from "./SecretTextareaField";
import PasswordField from "./PasswordField";

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

export default function VpnCredentialFields({
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
