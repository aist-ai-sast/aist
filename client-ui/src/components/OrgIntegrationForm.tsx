import { useEffect, useState } from "react";

import { toUserMessage } from "../lib/api";
import { useCreateOrgIntegration, useUpdateOrgIntegration, type OrgIntegrationPayload } from "../lib/mutations";
import type { OrgIntegration } from "../lib/queries";
import { useToast } from "./ToastProvider";
import Checkbox from "./Checkbox";
import DastIntegrationForm from "./DastIntegrationForm";
import {
  IntegrationFormFrame,
  ORG_INTEGRATION_DESCRIPTORS,
  formStateFor,
  trimmedConfig,
  type IntegrationDescriptor,
  type IntegrationFormProps,
  type IntegrationPayloadFields,
  type OrgIntegrationFormState,
} from "./OrgIntegrationDescriptors";

/** Saves any non-DAST type through the integration endpoint, as its descriptor describes it. */
function IntegrationSettingsForm({
  descriptor,
  orgId,
  editing,
  form,
  setForm,
  onDone,
}: IntegrationFormProps & { descriptor: IntegrationDescriptor<IntegrationPayloadFields> }) {
  const toast = useToast();
  const createIntegration = useCreateOrgIntegration(orgId);
  const updateIntegration = useUpdateOrgIntegration(orgId);

  async function handleSave() {
    const payload: OrgIntegrationPayload = {
      integration_type: form.integration_type,
      name: form.name.trim(),
      config: trimmedConfig(form.config),
      is_active: form.is_active,
      ...descriptor.payload(form),
    };
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

  return (
    <IntegrationFormFrame
      editing={editing}
      form={form}
      setForm={setForm}
      onSave={handleSave}
      onCancel={onDone}
      isPending={createIntegration.isPending || updateIntegration.isPending}
      isComplete={descriptor.isComplete(form, editing)}
    >
      {descriptor.renderFields({ orgId, editing, form, setForm })}
      <Checkbox
        className="sm:col-span-2"
        label="Active"
        checked={form.is_active}
        onChange={(e) => setForm((prev) => ({ ...prev, is_active: e.target.checked }))}
      />
    </IntegrationFormFrame>
  );
}

/** Creates or edits an organization integration; the chosen type picks the form that saves it. */
export default function OrgIntegrationForm({
  orgId,
  editing,
  onDone,
}: {
  orgId: number;
  editing: OrgIntegration | null;
  onDone: () => void;
}) {
  const [form, setForm] = useState<OrgIntegrationFormState>(() => formStateFor(editing));

  useEffect(() => {
    setForm(formStateFor(editing));
  }, [editing]);

  const formProps = { orgId, editing, form, setForm, onDone };
  const type = form.integration_type;
  if (type === "DAST") return <DastIntegrationForm {...formProps} />;
  return <IntegrationSettingsForm {...formProps} descriptor={ORG_INTEGRATION_DESCRIPTORS[type]} />;
}
