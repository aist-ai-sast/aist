import { useState, type ChangeEvent } from "react";

import TextInput from "./TextInput";

export default function PasswordField({
  value,
  onChange,
  placeholder,
  className,
  autoComplete,
  disabled,
}: {
  value: string;
  onChange: (e: ChangeEvent<HTMLInputElement>) => void;
  placeholder?: string;
  className?: string;
  autoComplete?: string;
  disabled?: boolean;
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
        autoComplete={autoComplete}
        disabled={disabled}
      />
      <button
        type="button"
        className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-200 transition"
        onClick={() => setShow((s) => !s)}
        tabIndex={-1}
        aria-label={show ? "Hide password" : "Show password"}
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
