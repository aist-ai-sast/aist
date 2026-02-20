import * as Popover from "@radix-ui/react-popover";
import { Link } from "react-router-dom";

import { logoutSession, useAuthStatus } from "../lib/auth";
import { useAccountProfile } from "../lib/account";
import { getRoute } from "../lib/routes";
import { getDisplayName, getInitials, getRoleLabel, getUsername } from "../lib/userProfile";
import { useToast } from "./ToastProvider";

type UserProfileMenuProps = {
  compact?: boolean;
};

export default function UserProfileMenu({ compact = false }: UserProfileMenuProps) {
  const toast = useToast();
  const auth = useAuthStatus();
  const account = useAccountProfile();

  const displayName = getDisplayName(auth.data);
  const username = getUsername(auth.data);
  const role = getRoleLabel(auth.data);
  const organizationRole = account.data?.organization_memberships?.[0]?.role_name ?? role;
  const initials = getInitials(auth.data);
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button
          className={[
            "inline-flex items-center gap-3 rounded-xl border border-night-500 bg-night-700/80 px-3 py-2 text-left transition hover:border-brand-600/50 hover:bg-night-700",
            compact ? "w-auto" : "w-full",
          ].join(" ")}
          aria-label="Open profile menu"
        >
          <span className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-brand-600/20 text-xs font-semibold text-brand-200">
            {initials}
          </span>
          {compact ? null : (
            <span className="min-w-0">
              <span className="block truncate text-sm text-slate-100">{displayName}</span>
              <span className="block truncate text-xs text-slate-400">@{username} · {organizationRole}</span>
            </span>
          )}
          <svg viewBox="0 0 24 24" className="h-4 w-4 text-slate-400" aria-hidden="true">
            <path fill="currentColor" d="M7 10l5 5 5-5H7Z" />
          </svg>
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="end"
          sideOffset={8}
          className="z-[1200] w-72 rounded-xl border border-night-500 bg-night-900 p-3 shadow-panel"
        >
          <div className="mb-3 rounded-lg border border-night-500 bg-night-800/80 px-3 py-2">
            <div className="truncate text-sm text-slate-100">{displayName}</div>
            <div className="truncate text-xs text-slate-400">@{username} · {organizationRole}</div>
          </div>
          <div className="space-y-1">
            <Link
              to={`${getRoute("ui_settings_path")}#account`}
              className="block rounded-lg px-3 py-2 text-sm text-slate-200 transition hover:bg-night-700"
            >
              My account
            </Link>
            <button
              className="mt-1 inline-flex w-full items-center gap-2 rounded-lg border border-night-500 bg-night-700 px-3 py-2 text-left text-sm text-slate-100 transition hover:border-brand-600/50"
              onClick={async () => {
                await logoutSession();
                toast.push("Signed out.", "success");
                window.location.reload();
              }}
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0" aria-hidden="true">
                <path d="M10 5H5v14h5v2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5v2Zm4.5 2.5L19 12l-4.5 4.5-1.4-1.4L15.2 13H9v-2h6.2l-2.1-2.1 1.4-1.4Z" fill="currentColor" />
              </svg>
              Sign out
            </button>
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
