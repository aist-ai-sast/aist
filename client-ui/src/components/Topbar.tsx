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
      <input
        className="w-full max-w-xl flex-1 rounded-xl border border-night-500 bg-night-700 px-4 py-2 text-sm text-white placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-brand-600"
        placeholder="Search findings, products, pipelines..."
      />
      <div className="hidden md:flex items-center gap-3 text-xs text-slate-300">
        <span className="rounded-full border border-night-500 bg-night-700 px-3 py-1">
          Scope: All Products
        </span>
        <span className="rounded-full border border-night-500 bg-night-700 px-3 py-1">
          Client Role
        </span>
        <button
          className="rounded-full border border-night-500 bg-night-700 px-3 py-1 text-xs text-slate-200"
          onClick={async () => {
            await logoutSession();
            toast.push("Signed out.", "success");
            window.location.reload();
          }}
        >
          Sign out
        </button>
      </div>
    </header>
  );
}
