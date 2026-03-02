type FilterClearButtonProps = {
  onClick: () => void;
  label?: string;
  disabled?: boolean;
  className?: string;
};

export default function FilterClearButton({
  onClick,
  label = "Clear",
  disabled = false,
  className,
}: FilterClearButtonProps) {
  return (
    <button
      type="button"
      className={[
        "text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-400 transition hover:text-brand-300 disabled:opacity-50",
        className ?? "",
      ].join(" ").trim()}
      onClick={onClick}
      disabled={disabled}
    >
      {label}
    </button>
  );
}
