import { forwardRef, type InputHTMLAttributes, type ReactNode } from "react";

type CheckboxProps = Omit<InputHTMLAttributes<HTMLInputElement>, "type"> & {
  label: ReactNode;
  description?: ReactNode;
};

const Checkbox = forwardRef<HTMLInputElement, CheckboxProps>(function Checkbox(
  { label, description, className, disabled, ...props },
  ref,
) {
  return (
    <label
      className={[
        "flex items-start gap-2 text-xs",
        disabled ? "cursor-not-allowed text-slate-500" : "cursor-pointer text-slate-300",
        className ?? "",
      ].join(" ")}
    >
      <input
        ref={ref}
        type="checkbox"
        disabled={disabled}
        className="mt-0.5 h-4 w-4 shrink-0 rounded border-night-500 bg-night-600 accent-brand-500 disabled:opacity-50"
        {...props}
      />
      <span>
        {label}
        {description ? <span className="mt-1 block text-xs text-slate-400">{description}</span> : null}
      </span>
    </label>
  );
});

export default Checkbox;
