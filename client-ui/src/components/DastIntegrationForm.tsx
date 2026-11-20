import { useEffect, useState } from "react";

import { toUserMessage } from "../lib/api";
import {
  useImportDastIntegration,
  useUpdateDastIntegrationOnboarding,
  type DastOnboardingBundle,
} from "../lib/mutations";
import { useToast } from "./ToastProvider";
import SecretTextareaField from "./SecretTextareaField";
import {
  IntegrationFormFrame,
  ORG_INTEGRATION_DESCRIPTORS,
  type IntegrationFormProps,
} from "./OrgIntegrationDescriptors";

const DAST = ORG_INTEGRATION_DESCRIPTORS.DAST;

/** Onboards a DAST integration from a signed bundle, pasted or entered field by field. */
export default function DastIntegrationForm({ orgId, editing, form, setForm, onDone }: IntegrationFormProps) {
  const toast = useToast();
  const importDastIntegration = useImportDastIntegration(orgId);
  const updateDastIntegration = useUpdateDastIntegrationOnboarding(orgId);
  const [dastBundleJson, setDastBundleJson] = useState("");

  useEffect(() => {
    setDastBundleJson("");
  }, [editing]);

  function loadDastBundle() {
    try {
      const bundle = JSON.parse(dastBundleJson) as DastOnboardingBundle;
      if (bundle.bundle_version !== 1 || bundle.contract_major !== 2) {
        throw new Error("Only bundle_version 1 with contract_major 2 is supported.");
      }
      setForm((previous) => ({
        ...previous,
        integration_type: "DAST",
        secret: bundle.token,
        config: {
          gateway_url: bundle.gateway_url,
          ca_bundle: bundle.ca_bundle,
          integrator_public_id: bundle.integrator_public_id,
          server_fingerprint: bundle.server_fingerprint,
        },
      }));
    } catch (error) {
      toast.push(`Bundle parse failed: ${toUserMessage(error)}`, "error");
    }
  }

  async function handleSave() {
    const bundle = DAST.payload(form);
    try {
      if (editing) {
        await updateDastIntegration.mutateAsync({
          integrationId: editing.id,
          payload: {
            name: form.name.trim(),
            vpn_integration_id: form.vpn_integration,
            ...(bundle ? { bundle } : {}),
          },
        });
        toast.push("DAST integration updated.", "success");
      } else if (bundle) {
        await importDastIntegration.mutateAsync({
          name: form.name.trim(),
          vpn_integration_id: form.vpn_integration,
          bundle,
        });
        toast.push("DAST onboarding bundle imported.", "success");
      }
      setForm((previous) => ({ ...previous, secret: "" }));
      setDastBundleJson("");
      importDastIntegration.reset();
      updateDastIntegration.reset();
      onDone();
    } catch (error) {
      toast.push(toUserMessage(error), "error");
    }
  }

  return (
    <IntegrationFormFrame
      editing={editing}
      form={form}
      setForm={setForm}
      onSave={handleSave}
      onCancel={onDone}
      isPending={importDastIntegration.isPending || updateDastIntegration.isPending}
      isComplete={DAST.isComplete(form, editing)}
    >
      <div className="sm:col-span-2 rounded-xl border border-night-500/60 bg-night-800/50 p-3">
        <div className="text-xs font-medium text-slate-300">Import onboarding bundle</div>
        <p className="mt-1 text-[11px] text-slate-500">
          Paste the protected JSON emitted by the DAST deployment, or enter the same fields manually below.
        </p>
        {editing && (
          <p className="mt-1 text-[11px] text-slate-500">
            Leave this empty to keep the stored connection and change only the name or VPN route.
            Changing the gateway URL, CA bundle, integrator id, or fingerprint requires the
            integrator token that belongs to them.
          </p>
        )}
        <SecretTextareaField
          className="mt-2"
          rows={5}
          value={dastBundleJson}
          onChange={(event) => setDastBundleJson(event.target.value)}
          placeholder='{"bundle_version":1,"gateway_url":"https://..."}'
        />
        <button type="button" className="aist-icon-button mt-2" onClick={loadDastBundle}>
          Load bundle
        </button>
      </div>
      {DAST.renderFields({ orgId, editing, form, setForm })}
    </IntegrationFormFrame>
  );
}
