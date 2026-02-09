export default function Topbar() {
  return (
    <header className="sticky top-0 z-20 flex items-center justify-between border-b border-night-500 bg-night-900/90 px-4 py-3 lg:px-6 lg:py-4">
      <div className="flex items-center gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-xl border border-night-500 bg-night-700">
          <img src="/logo.svg" alt="AIST" className="h-6 w-6 object-contain" />
        </div>
        <div className="text-xs uppercase tracking-[0.3em] text-slate-400 leading-none">
          Client Portal
        </div>
      </div>
    </header>
  );
}
