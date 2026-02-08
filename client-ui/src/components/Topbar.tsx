import { logoutSession } from "../lib/auth";
import { useToast } from "./ToastProvider";

export default function Topbar() {
  const toast = useToast();
  return (
    <header className="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-4 border-b border-night-500 bg-night-900/90 px-6 py-4">
      <div className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-night-500 bg-night-700">
          <img src="/logo.svg" alt="AIST" className="h-7 w-7 object-contain" />
        </div>
        <div className="text-xs uppercase tracking-[0.3em] text-slate-400">AIST</div>
      </div>
      <div className="relative w-full max-w-xl flex-1">
        <input
          className="w-full rounded-xl border border-night-500 bg-night-700 pl-10 pr-4 py-2 text-sm text-white placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-brand-600"
          placeholder="Search findings, products, pipelines..."
        />
        <div className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400">
          <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
            <path
              fill="currentColor"
              d="M10.5 3a7.5 7.5 0 1 1-4.59 13.43l-2.7 2.7-1.06-1.06 2.7-2.7A7.5 7.5 0 0 1 10.5 3Zm0 1.5a6 6 0 1 0 0 12a6 6 0 0 0 0-12Z"
            />
          </svg>
        </div>
      </div>
      <div className="hidden md:flex items-center gap-3 text-xs text-slate-300">
        <button
          className="rounded-full border border-night-500 bg-night-700 px-3 py-1 text-xs text-slate-200 inline-flex items-center gap-2"
          onClick={async () => {
            await logoutSession();
            toast.push("Signed out.", "success");
            window.location.reload();
          }}
        >
          <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
            <path
              fill="currentColor"
              d="M10 5H5v14h5v2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5v2Zm4.5 2.5L19 12l-4.5 4.5-1.4-1.4L15.2 13H9v-2h6.2l-2.1-2.1 1.4-1.4Z"
            />
          </svg>
          Sign out
        </button>
      </div>
    </header>
  );
}
