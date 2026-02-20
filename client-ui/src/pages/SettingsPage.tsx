import { useEffect, useState } from "react";

import PageErrorState from "../components/PageErrorState";
import { logoutSession, useAuthStatus } from "../lib/auth";
import { logoutAllDevices, useAccountProfile, useChangePassword, useUpdateAccountProfile } from "../lib/account";
import { toUserMessage } from "../lib/api";
import { getDisplayName, getRoleLabel, getUsername } from "../lib/userProfile";
import { usePermissions } from "../lib/permissions";
import { useToast } from "../components/ToastProvider";

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="aist-card border-night-500/80 p-0">
      <div className="border-b border-night-500/70 px-5 py-4">
        <div className="text-xs uppercase tracking-[0.2em] text-slate-400">{title}</div>
      </div>
      <div className="px-5 py-4">{children}</div>
    </section>
  );
}

const accountInputClass =
  "mt-1 w-full rounded-xl border border-night-500 bg-night-800/90 px-3 py-2 text-sm text-slate-100 shadow-inner shadow-night-900/30 outline-none transition disabled:opacity-60 focus:border-brand-500/60 focus:ring-2 focus:ring-brand-500/30 focus-visible:outline-none";

export default function SettingsPage() {
  const auth = useAuthStatus();
  const accountQuery = useAccountProfile();
  const updateAccount = useUpdateAccountProfile();
  const changePassword = useChangePassword();
  const permissions = usePermissions();
  const toast = useToast();
  const [loadingAllDevicesLogout, setLoadingAllDevicesLogout] = useState(false);
  const [profileForm, setProfileForm] = useState({
    first_name: "",
    last_name: "",
    email: "",
    username: "",
  });
  const [passwordForm, setPasswordForm] = useState({
    current_password: "",
    new_password: "",
    new_password_confirm: "",
  });
  const [showCurrentPassword, setShowCurrentPassword] = useState(false);
  const [showNewPassword, setShowNewPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);

  useEffect(() => {
    if (!accountQuery.data) return;
    setProfileForm({
      first_name: accountQuery.data.first_name ?? "",
      last_name: accountQuery.data.last_name ?? "",
      email: accountQuery.data.email ?? "",
      username: accountQuery.data.username ?? "",
    });
  }, [accountQuery.data]);

  if (auth.isLoading) {
    return (
      <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
        Loading settings...
      </div>
    );
  }

  if (auth.isError) {
    return <PageErrorState error={auth.error} fallbackTitle="Failed to load settings" />;
  }
  if (accountQuery.isLoading) {
    return (
      <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
        Loading account...
      </div>
    );
  }
  if (accountQuery.isError || !accountQuery.data) {
    return <PageErrorState error={accountQuery.error} fallbackTitle="Failed to load account data" />;
  }

  const displayName = getDisplayName(auth.data);
  const username = getUsername(auth.data);
  const role = getRoleLabel(auth.data);
  const canManageProfile = permissions.canWrite || permissions.canManageAccess;
  const canEditProfile = canManageProfile && accountQuery.data.can_edit_profile;
  const canEditUsername = canManageProfile && accountQuery.data.can_edit_username;

  return (
    <div className="space-y-6">
      <div>
        <div className="text-xs uppercase tracking-[0.2em] text-slate-400">My Account</div>
        <h1 className="mt-2 text-2xl font-semibold text-white">{displayName}</h1>
        <div className="mt-1 text-xs text-slate-400">@{username} · {role}</div>
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.2fr_1fr]">
        <section id="account">
          <Card title="Account">
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="text-xs text-slate-400">
                First name
                <input
                  className={accountInputClass}
                  value={profileForm.first_name}
                  disabled={!canEditProfile || updateAccount.isPending}
                  onChange={(event) => setProfileForm((prev) => ({ ...prev, first_name: event.target.value }))}
                />
              </label>
              <label className="text-xs text-slate-400">
                Last name
                <input
                  className={accountInputClass}
                  value={profileForm.last_name}
                  disabled={!canEditProfile || updateAccount.isPending}
                  onChange={(event) => setProfileForm((prev) => ({ ...prev, last_name: event.target.value }))}
                />
              </label>
              <label className="text-xs text-slate-400 sm:col-span-2">
                Email
                <input
                  type="text"
                  inputMode="email"
                  autoComplete="email"
                  className={accountInputClass}
                  value={profileForm.email}
                  disabled={!canEditProfile || updateAccount.isPending}
                  onChange={(event) => setProfileForm((prev) => ({ ...prev, email: event.target.value }))}
                />
              </label>
              <label className="text-xs text-slate-400 sm:col-span-2">
                Username
                <input
                  className={accountInputClass}
                  value={profileForm.username}
                  disabled={!canEditUsername || updateAccount.isPending}
                  onChange={(event) => setProfileForm((prev) => ({ ...prev, username: event.target.value }))}
                />
              </label>
            </div>
            {canEditProfile ? (
              <div className="mt-4 flex gap-2">
                <button
                  className="aist-icon-button border-brand-500/50 bg-brand-500/15 text-brand-100 hover:border-brand-400/70 hover:bg-brand-500/25"
                  disabled={updateAccount.isPending}
                  onClick={async () => {
                    try {
                      await updateAccount.mutateAsync(profileForm);
                      toast.push("Account updated.", "success");
                    } catch (error) {
                      toast.push(toUserMessage(error), "error");
                    }
                  }}
                >
                  <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
                    <path fill="currentColor" d="M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z" />
                  </svg>
                  Save changes
                </button>
                <button
                  className="aist-icon-button border-night-400/80 bg-night-800/80 text-slate-200"
                  disabled={updateAccount.isPending}
                  onClick={() => {
                    setProfileForm({
                      first_name: accountQuery.data.first_name,
                      last_name: accountQuery.data.last_name,
                      email: accountQuery.data.email,
                      username: accountQuery.data.username,
                    });
                  }}
                >
                  <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
                    <path fill="currentColor" d="M18.3 5.7 12 12l6.3 6.3-1.4 1.4L10.6 13.4 4.3 19.7l-1.4-1.4L9.2 12 2.9 5.7l1.4-1.4 6.3 6.3 6.3-6.3z" />
                  </svg>
                  Cancel
                </button>
              </div>
            ) : (
              <p className="mt-4 text-xs text-slate-400">Profile editing is restricted by role or policy.</p>
            )}
          </Card>
        </section>

        <div className="space-y-4">
          <section id="security">
            <Card title="Session & Security">
              <div className="space-y-4 text-sm text-slate-200">
                <div className="grid gap-3">
                <label className="text-xs text-slate-400">
                  Current password
                  <div className="relative">
                    <input
                      type={showCurrentPassword ? "text" : "password"}
                      autoComplete="current-password"
                      className={`${accountInputClass} pr-12`}
                      value={passwordForm.current_password}
                      onChange={(event) => setPasswordForm((prev) => ({ ...prev, current_password: event.target.value }))}
                    />
                    <button
                      type="button"
                      className="absolute right-2 top-1/2 -translate-y-1/2 rounded-md px-2 py-1 text-[11px] text-slate-400 transition hover:bg-night-700 hover:text-slate-200"
                      onClick={() => setShowCurrentPassword((value) => !value)}
                    >
                      {showCurrentPassword ? "Hide" : "Show"}
                    </button>
                  </div>
                </label>
                <label className="text-xs text-slate-400">
                  New password
                  <div className="relative">
                    <input
                      type={showNewPassword ? "text" : "password"}
                      autoComplete="new-password"
                      className={`${accountInputClass} pr-12`}
                      value={passwordForm.new_password}
                      onChange={(event) => setPasswordForm((prev) => ({ ...prev, new_password: event.target.value }))}
                    />
                    <button
                      type="button"
                      className="absolute right-2 top-1/2 -translate-y-1/2 rounded-md px-2 py-1 text-[11px] text-slate-400 transition hover:bg-night-700 hover:text-slate-200"
                      onClick={() => setShowNewPassword((value) => !value)}
                    >
                      {showNewPassword ? "Hide" : "Show"}
                    </button>
                  </div>
                </label>
                <label className="text-xs text-slate-400">
                  Confirm new password
                  <div className="relative">
                    <input
                      type={showConfirmPassword ? "text" : "password"}
                      autoComplete="new-password"
                      className={`${accountInputClass} pr-12`}
                      value={passwordForm.new_password_confirm}
                      onChange={(event) => setPasswordForm((prev) => ({ ...prev, new_password_confirm: event.target.value }))}
                    />
                    <button
                      type="button"
                      className="absolute right-2 top-1/2 -translate-y-1/2 rounded-md px-2 py-1 text-[11px] text-slate-400 transition hover:bg-night-700 hover:text-slate-200"
                      onClick={() => setShowConfirmPassword((value) => !value)}
                    >
                      {showConfirmPassword ? "Hide" : "Show"}
                    </button>
                  </div>
                </label>
                <div className="self-start">
                  <button
                    className="aist-icon-button"
                    disabled={changePassword.isPending}
                    onClick={async () => {
                      try {
                        await changePassword.mutateAsync(passwordForm);
                        toast.push("Password changed.", "success");
                        setPasswordForm({
                          current_password: "",
                          new_password: "",
                          new_password_confirm: "",
                        });
                      } catch (error) {
                        toast.push(toUserMessage(error), "error");
                      }
                    }}
                  >
                    <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
                      <path fill="currentColor" d="m12 2 8 4v6c0 5.5-3.8 10.7-8 12-4.2-1.3-8-6.5-8-12V6l8-4Zm0 11.3 4.2-4.2 1.4 1.4-5.6 5.6-3-3 1.4-1.4 1.6 1.6Z" />
                    </svg>
                    Change password
                  </button>
                </div>
              </div>
                <div className="border-t border-night-500 pt-4">
                  <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Sessions</div>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <button
                      className="aist-icon-button"
                      onClick={async () => {
                        await logoutSession();
                        toast.push("Signed out.", "success");
                        window.location.reload();
                      }}
                    >
                      <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
                        <path fill="currentColor" d="M10 5H5v14h5v2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5v2Zm4.5 2.5L19 12l-4.5 4.5-1.4-1.4L15.2 13H9v-2h6.2l-2.1-2.1 1.4-1.4Z" />
                      </svg>
                      Sign out current device
                    </button>
                    <button
                      className="inline-flex items-center gap-2 rounded-xl border border-danger-500/70 bg-danger-500/10 px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.09em] text-danger-500 transition hover:bg-danger-500/20 disabled:opacity-60"
                      disabled={loadingAllDevicesLogout}
                      onClick={async () => {
                        setLoadingAllDevicesLogout(true);
                        try {
                          await logoutAllDevices();
                          toast.push("Signed out from all devices.", "success");
                          window.location.reload();
                        } finally {
                          setLoadingAllDevicesLogout(false);
                        }
                      }}
                    >
                      <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
                        <path fill="currentColor" d="M12 2 1 21h22L12 2Zm1 15h-2v2h2v-2Zm0-7h-2v5h2v-5Z" />
                      </svg>
                      Sign out all devices
                    </button>
                  </div>
                </div>
              </div>
            </Card>
          </section>

          <Card title="Access">
            <div className="space-y-3 text-sm text-slate-200">
              <p>Your resource access is managed by your organization administrators.</p>
              <p className="text-xs text-slate-400">
                To request additional access, contact your security or platform administrator.
              </p>
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}
