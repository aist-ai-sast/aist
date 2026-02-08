import * as Select from "@radix-ui/react-select";

type Option = {
  value: string;
  label: string;
  disabled?: boolean;
};

type SelectFieldProps = {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: Option[];
  disabled?: boolean;
  placeholder?: string;
  hideLabel?: boolean;
};

export default function SelectField({
  label,
  value,
  onChange,
  options,
  disabled,
  placeholder = "Select",
  hideLabel = false,
}: SelectFieldProps) {
  const selected = options.find((option) => option.value === value);
  return (
    <div>
      {hideLabel ? null : <label className="text-xs text-slate-400">{label}</label>}
      <Select.Root value={value || undefined} onValueChange={onChange} disabled={disabled}>
        <Select.Trigger
          className={[
            "flex w-full items-center justify-between rounded-xl border border-night-500 bg-night-600 px-3 py-2 text-sm text-white",
            hideLabel ? "mt-0" : "mt-2",
          ].join(" ")}
        >
          <Select.Value placeholder={placeholder}>
            {selected?.label}
          </Select.Value>
          <Select.Icon className="text-slate-400">
            <svg
              width="16"
              height="16"
              viewBox="0 0 20 20"
              fill="currentColor"
              aria-hidden="true"
            >
              <path d="M5.25 7.5 10 12.25 14.75 7.5H5.25Z" />
            </svg>
          </Select.Icon>
        </Select.Trigger>
        <Select.Portal>
          <Select.Content
            position="popper"
            className="z-50 overflow-hidden rounded-xl border border-night-500 bg-night-900 shadow-panel"
            style={{ minWidth: "var(--radix-select-trigger-width)" }}
          >
            <Select.Viewport className="p-1">
              {options.map((option) => (
                <Select.Item
                  key={option.value}
                  value={option.value}
                  disabled={option.disabled}
                  className="cursor-pointer select-none rounded-lg px-3 py-2 text-sm text-slate-200 outline-none data-[highlighted]:bg-night-700 data-[state=checked]:bg-night-600 data-[disabled]:text-slate-500"
                >
                  <Select.ItemText>{option.label}</Select.ItemText>
                </Select.Item>
              ))}
            </Select.Viewport>
          </Select.Content>
        </Select.Portal>
      </Select.Root>
    </div>
  );
}
