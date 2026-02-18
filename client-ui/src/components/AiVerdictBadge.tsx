import type { AIVerdict } from "../types";

type AiVerdictBadgeProps = {
  verdict?: AIVerdict;
  className?: string;
};

const verdictLabel: Record<AIVerdict, string> = {
  true_positive: "AI TP",
  false_positive: "AI FP",
  uncertain: "AI U?",
};

const verdictStyle: Record<AIVerdict, string> = {
  true_positive: "border-danger-500/35 bg-danger-500/10 text-danger-200",
  false_positive: "border-emerald-500/35 bg-emerald-500/10 text-emerald-200",
  uncertain: "border-amber-400/35 bg-amber-400/10 text-amber-200",
};

export default function AiVerdictBadge({ verdict, className = "" }: AiVerdictBadgeProps) {
  if (!verdict) return null;

  return (
    <span
      className={[
        "rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-wide",
        verdictStyle[verdict],
        className,
      ].join(" ")}
      title={verdictLabel[verdict]}
    >
      {verdictLabel[verdict]}
    </span>
  );
}
