type PlaceholderPageProps = {
  title: string;
  description: string;
};

export default function PlaceholderPage({ title, description }: PlaceholderPageProps) {
  return (
    <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
      <div className="text-xs uppercase tracking-[0.2em] text-slate-400">{title}</div>
      <p className="mt-3">{description}</p>
    </div>
  );
}
