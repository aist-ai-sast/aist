import { forwardRef, type InputHTMLAttributes } from "react";

type TextInputVariant = "default" | "password" | "compact";

type TextInputProps = InputHTMLAttributes<HTMLInputElement> & {
  variant?: TextInputVariant;
};

const defaultInputClass =
  "h-10 w-full rounded-xl border border-night-500 bg-night-600 px-3 text-sm text-white placeholder:text-slate-400 outline-none transition focus:outline-none focus:ring-0 focus:border-brand-600/70 focus-visible:border-brand-600/70 focus-visible:shadow-[0_0_0_1px_rgba(77,212,255,0.28)]";
const passwordInputClass =
  "h-10 w-full rounded-xl border border-night-500 bg-night-800/90 px-3 text-sm text-slate-100 shadow-inner shadow-night-900/30 outline-none transition focus:outline-none focus:ring-0 focus:border-brand-500/60 focus-visible:border-brand-500/60 focus-visible:shadow-[0_0_0_1px_rgba(77,212,255,0.22)]";
const compactInputClass =
  "h-9 w-full rounded-xl border border-night-500 bg-night-800 px-2 text-xs text-slate-200 placeholder:text-slate-500 outline-none transition focus:outline-none focus:ring-0 focus:border-brand-600/70 focus-visible:border-brand-600/70 focus-visible:shadow-[0_0_0_1px_rgba(77,212,255,0.24)]";

const TextInput = forwardRef<HTMLInputElement, TextInputProps>(function TextInput(
  { className, type = "text", variant = "default", ...props },
  ref,
) {
  const variantClass = (
    {
      default: defaultInputClass,
      password: passwordInputClass,
      compact: compactInputClass,
    } as const
  )[variant];
  return (
    <input
      ref={ref}
      type={type}
      className={className ? `${variantClass} ${className}` : variantClass}
      {...props}
    />
  );
});

export default TextInput;
