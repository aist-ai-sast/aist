import { useState } from "react";

import { loginWithSession } from "../lib/auth";
import { useToast } from "../components/ToastProvider";

export default function LoginPage({ onSuccess }: { onSuccess: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const toast = useToast();

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-night-800 px-6">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top_left,rgba(77,212,255,0.25),transparent_45%),radial-gradient(circle_at_bottom_right,rgba(248,199,77,0.18),transparent_50%)]" />
      <div className="relative grid w-full max-w-5xl gap-10 rounded-[28px] border border-night-500/80 bg-night-700/90 p-10 shadow-panel lg:grid-cols-[1.1fr_0.9fr]">
        <section>
          <div className="flex items-center gap-4">
            <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-night-900">
              <img src="/logo.svg" alt="AIST logo" className="h-12 w-12 object-contain" />
            </div>
            <div>
              <div className="text-xs uppercase tracking-[0.3em] text-slate-400">AIST</div>
              <h1 className="mt-2 text-2xl font-semibold text-white">Client Security Portal</h1>
            </div>
          </div>
          <p className="mt-6 text-sm text-slate-300">
            Secure access for client stakeholders. Monitor findings, review AI triage,
            and track pipeline history with governance-grade visibility.
          </p>
          <div className="mt-6 rounded-2xl border border-night-500 bg-night-900/80 p-5 text-xs text-slate-300">
            <div className="text-[11px] uppercase tracking-[0.2em] text-slate-500">Support</div>
            <p className="mt-3 text-sm text-slate-300">
              Need access or a password reset? Contact your security program administrator.
            </p>
          </div>
        </section>

        <section className="rounded-2xl border border-night-500 bg-night-900/80 p-6">
          <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Sign in</div>
          <p className="mt-2 text-sm text-slate-300">
            Use your DefectDojo credentials to access the client UI.
          </p>

          <form
            className="mt-6 space-y-4"
            onSubmit={async (event) => {
              event.preventDefault();
              setLoading(true);
              try {
                await loginWithSession(username, password);
                toast.push("Welcome back.", "success");
                onSuccess();
              } catch (error) {
                const message = error instanceof Error ? error.message : String(error);
                toast.push(message, "error");
              } finally {
                setLoading(false);
              }
            }}
          >
            <div>
              <label className="text-xs text-slate-400">Username</label>
              <input
                className="mt-2 w-full rounded-xl border border-night-500 bg-night-900 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-brand-600"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                placeholder="username"
                autoComplete="username"
                required
              />
            </div>
            <div>
              <label className="text-xs text-slate-400">Password</label>
              <input
                className="mt-2 w-full rounded-xl border border-night-500 bg-night-900 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-brand-600"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder="password"
                type="password"
                autoComplete="current-password"
                required
              />
            </div>
            <button
              className="mt-4 w-full rounded-xl bg-brand-500 px-4 py-2 text-sm font-semibold text-night-900 disabled:opacity-50"
              type="submit"
              disabled={loading}
            >
              {loading ? "Signing in..." : "Sign in"}
            </button>
          </form>
        </section>
      </div>
    </div>
  );
}
