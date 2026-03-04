type SkeletonBlockProps = {
  className?: string;
};

function SkeletonLine({ className }: SkeletonBlockProps) {
  return (
    <div
      className={[
        "h-3 animate-pulse rounded-full bg-night-500/60",
        className ?? "",
      ].join(" ").trim()}
    />
  );
}

/** Generic card-shaped skeleton used while data is loading. */
export default function SkeletonBlock({ className }: SkeletonBlockProps) {
  return (
    <div
      className={[
        "rounded-2xl border border-night-500 bg-night-700 p-5",
        className ?? "",
      ].join(" ").trim()}
      aria-busy="true"
      aria-label="Loading…"
    >
      <div className="space-y-3">
        <SkeletonLine className="w-24" />
        <SkeletonLine className="w-3/4" />
        <SkeletonLine className="w-1/2" />
      </div>
    </div>
  );
}

export { SkeletonLine };
