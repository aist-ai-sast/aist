import { useMemo, useState } from "react";

import PasswordField from "../components/PasswordField";
import { fetchJson, toUserMessage } from "../lib/api";
import { getRoute } from "../lib/routes";

function parseUidToken(): { uid: string; token: string } {
  // Path: /auth/set-password/<uid>/<token>/
  const parts = window.location.pathname.split("/").filter(Boolean);
  const idx = parts.indexOf("set-password");
  return { uid: parts[idx + 1] ?? "", token: parts[idx + 2] ?? "" };
}

export default function SetPasswordPage() {
  const { uid, token } = useMemo(parseUidToken, []);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    setError(null);
    if (password !== confirm) {
      setError("Passwords do not match.");
      return;
    }
    setSubmitting(true);
    try {
      await fetchJson(getRoute("set_password_api_url"), {
        method: "POST",
        body: JSON.stringify({ uid, token, new_password: password, new_password_confirm: confirm }),
      });
      setDone(true);
    } catch (err) {
      setError(toUserMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="aist-card w-full max-w-md border-night-500/80 p-6">
        <img src="/logo.svg" alt="AIST logo" className="h-10 w-10 object-contain" />
        <div className="mt-3 text-xs uppercase tracking-[0.3em] text-brand-500">AIST</div>
        <h1 className="mt-2 text-xl font-semibold text-white">Set your password</h1>

        {done ? (
          <div className="mt-4 space-y-4 text-sm text-slate-200">
            <p className="rounded-xl border border-brand-500/40 bg-brand-500/10 p-3 text-brand-100">
              Your password has been set. You can now sign in.
            </p>
            <a href={getRoute("login_url")} className="aist-icon-button inline-flex">
              Go to sign in
            </a>
          </div>
        ) : (
          <div className="mt-4 space-y-3 text-sm text-slate-200">
            <label className="block text-xs text-slate-400">
              New password
              <PasswordField
                autoComplete="new-password"
                className="mt-1"
                value={password}
                disabled={submitting}
                onChange={(event) => setPassword(event.target.value)}
              />
            </label>
            <label className="block text-xs text-slate-400">
              Confirm password
              <PasswordField
                autoComplete="new-password"
                className="mt-1"
                value={confirm}
                disabled={submitting}
                onChange={(event) => setConfirm(event.target.value)}
              />
            </label>

            {error ? (
              <p className="rounded-xl border border-danger-500/40 bg-danger-500/10 p-3 text-xs text-danger-500">{error}</p>
            ) : null}

            <button className="aist-icon-button w-full justify-center" disabled={submitting} onClick={submit}>
              {submitting ? "Setting..." : "Set password"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
