import Form, { type IChangeEvent } from "@rjsf/core";
import type {
  DescriptionFieldProps,
  FieldTemplateProps,
  RJSFSchema,
  RegistryWidgetsType,
  TitleFieldProps,
  WidgetProps,
} from "@rjsf/utils";
import validator from "@rjsf/validator-ajv8";
import type { ReactNode } from "react";

import Checkbox from "./Checkbox";
import SelectField from "./SelectField";
import TextInput from "./TextInput";

/**
 * Renders a provider-supplied JSON Schema through the app's own field components.
 *
 * A schema field is indistinguishable from a hand-written one: same select, same input,
 * same label and description scale. Widgets carry their own labels, so a new field type
 * only has to be added here once to inherit all of it.
 */

function humanize(value: string): string {
  const spaced = value.replace(/[_-]+/g, " ").replace(/([a-z\d])([A-Z])/g, "$1 $2").trim();
  return spaced ? spaced.charAt(0).toUpperCase() + spaced.slice(1) : value;
}

function fieldLabel({ label, schema, name }: Pick<WidgetProps, "label" | "schema" | "name">): string {
  const title = typeof schema.title === "string" ? schema.title.trim() : "";
  return title || humanize(label || name || "");
}

function FieldLabel({ htmlFor, text, children }: { htmlFor: string; text: string; children: ReactNode }) {
  return (
    <div>
      <label htmlFor={htmlFor} className="text-xs text-slate-400">
        {text}
      </label>
      <div className="mt-2">{children}</div>
    </div>
  );
}

function SchemaTextWidget(props: WidgetProps) {
  const { id, value, required, disabled, readonly, onChange, onBlur, onFocus, schema, placeholder } = props;
  const isNumeric = schema.type === "number" || schema.type === "integer";
  return (
    <FieldLabel htmlFor={id} text={fieldLabel(props)}>
      <TextInput
        id={id}
        type={isNumeric ? "number" : "text"}
        value={value ?? ""}
        required={required}
        disabled={disabled || readonly}
        placeholder={placeholder}
        onChange={(event) => {
          const raw = event.target.value;
          if (raw === "") {
            onChange(undefined);
            return;
          }
          onChange(isNumeric ? Number(raw) : raw);
        }}
        onBlur={(event) => onBlur?.(id, event.target.value)}
        onFocus={(event) => onFocus?.(id, event.target.value)}
      />
    </FieldLabel>
  );
}

function SchemaTextareaWidget(props: WidgetProps) {
  const { id, value, required, disabled, readonly, onChange, placeholder } = props;
  return (
    <FieldLabel htmlFor={id} text={fieldLabel(props)}>
      <textarea
        id={id}
        rows={4}
        value={value ?? ""}
        required={required}
        disabled={disabled || readonly}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value === "" ? undefined : event.target.value)}
        className="w-full rounded-xl border border-night-500 bg-night-600 px-3 py-2 text-sm text-white placeholder:text-slate-400 outline-none transition focus:outline-none focus:border-brand-600/70"
      />
    </FieldLabel>
  );
}

function SchemaSelectWidget(props: WidgetProps) {
  const { value, disabled, readonly, onChange, options, placeholder } = props;
  const enumOptions = (options.enumOptions ?? []) as Array<{ value: unknown; label: string }>;
  return (
    <SelectField
      label={fieldLabel(props)}
      value={value === undefined || value === null ? "" : String(value)}
      disabled={disabled || readonly}
      placeholder={placeholder || "Select"}
      onChange={(next) => {
        const match = enumOptions.find((option) => String(option.value) === next);
        onChange(match ? match.value : next);
      }}
      options={enumOptions.map((option) => ({
        value: String(option.value),
        // A provider that gives no oneOf/enumNames title leaves us the raw value; show it
        // the way every other option in the app reads rather than as a bare identifier.
        label: option.label && option.label !== String(option.value) ? option.label : humanize(String(option.value)),
      }))}
    />
  );
}

function SchemaCheckboxWidget(props: WidgetProps) {
  const { id, value, disabled, readonly, onChange } = props;
  return (
    <Checkbox
      id={id}
      label={fieldLabel(props)}
      checked={Boolean(value)}
      disabled={disabled || readonly}
      onChange={(event) => onChange(event.target.checked)}
    />
  );
}

const widgets: RegistryWidgetsType = {
  TextWidget: SchemaTextWidget,
  TextareaWidget: SchemaTextareaWidget,
  SelectWidget: SchemaSelectWidget,
  CheckboxWidget: SchemaCheckboxWidget,
};

function SchemaFieldTemplate({ children, rawDescription, rawErrors }: FieldTemplateProps) {
  return (
    <div className="space-y-1">
      {children}
      {rawDescription ? <p className="text-xs text-slate-400">{rawDescription}</p> : null}
      {rawErrors?.length ? (
        <ul className="space-y-0.5">
          {rawErrors.map((error) => (
            <li key={error} className="text-xs text-danger-400">
              {error}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function SchemaDescriptionTemplate({ description }: DescriptionFieldProps) {
  return description ? <p className="text-xs text-slate-400">{description}</p> : null;
}

function SchemaTitleTemplate({ title }: TitleFieldProps) {
  return title ? <span className="text-xs font-medium text-slate-300">{humanize(title)}</span> : null;
}

const templates = {
  FieldTemplate: SchemaFieldTemplate,
  DescriptionFieldTemplate: SchemaDescriptionTemplate,
  TitleFieldTemplate: SchemaTitleTemplate,
};

type SchemaFormProps = {
  schema: RJSFSchema;
  formData: Record<string, unknown>;
  onChange: (event: IChangeEvent<Record<string, unknown>>) => void;
  onSubmit: (event: IChangeEvent<Record<string, unknown>>) => void;
  formKey?: string;
  children?: ReactNode;
};

export default function SchemaForm({ schema, formData, onChange, onSubmit, formKey, children }: SchemaFormProps) {
  return (
    <Form
      key={formKey}
      schema={schema}
      validator={validator}
      formData={formData}
      liveValidate
      showErrorList={false}
      widgets={widgets}
      templates={templates}
      uiSchema={{ "ui:submitButtonOptions": { norender: true } }}
      onChange={(event) => onChange(event as IChangeEvent<Record<string, unknown>>)}
      onSubmit={(event) => onSubmit(event as IChangeEvent<Record<string, unknown>>)}
      className="space-y-3"
    >
      {children}
    </Form>
  );
}
