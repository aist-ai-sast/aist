import { useState, type ReactNode } from "react";
import * as Popover from "@radix-ui/react-popover";
import { NavLink, matchPath, useLocation } from "react-router-dom";
import { getRoute } from "../lib/routes";
import { useManageableOrgs } from "../lib/queries";
import { ObjectIcons } from "./ObjectIcons";

type SidebarProps = {
  collapsed: boolean;
  onToggle: () => void;
};

/**
 * Where a destination lives below `lg`, where the sidebar becomes a bottom bar. The bar holds
 * four primary destinations plus "More" (Material 3 / Apple HIG keep a bottom bar to 3–5);
 * the rest open from the More sheet. "none" is reachable elsewhere on phones (the profile menu).
 */
type MobilePlacement = "primary" | "more" | "none";

type NavDestination = {
  to: string;
  label: string;
  icon: ReactNode;
  mobile: MobilePlacement;
};

const NAV_LINK_CLASS = (collapsed: boolean, mobile: MobilePlacement) =>
  ({ isActive }: { isActive: boolean }) =>
    [
      "rounded-xl transition flex-col items-center justify-center gap-1 py-1.5 text-[12px] font-medium",
      "lg:flex-row lg:gap-2 lg:text-sm lg:font-normal",
      mobile === "primary" ? "flex" : "hidden lg:flex",
      collapsed
        ? "lg:w-10 lg:px-0 lg:py-3 lg:justify-center"
        : "lg:px-3 lg:py-3 lg:justify-start lg:text-left lg:w-full",
      isActive
        ? "text-white lg:bg-night-600"
        : "text-slate-400 hover:text-white lg:hover:bg-night-700",
    ].join(" ");

/** Icon holder: on phones it is the active-state pill, on desktop the link itself is highlighted. */
function NavIcon({ icon, active }: { icon: ReactNode; active: boolean }) {
  return (
    <span
      className={[
        "flex h-7 w-14 items-center justify-center rounded-full lg:h-auto lg:w-auto lg:bg-transparent lg:text-slate-400",
        active ? "bg-night-600 text-white" : "text-slate-400",
      ].join(" ")}
    >
      {icon}
    </span>
  );
}

function NavItem({ destination, collapsed }: { destination: NavDestination; collapsed: boolean }) {
  return (
    <NavLink to={destination.to} className={NAV_LINK_CLASS(collapsed, destination.mobile)}>
      {({ isActive }) => (
        <>
          <NavIcon icon={destination.icon} active={isActive} />
          {/* Always rendered so the link keeps an accessible name; visually hidden only in the collapsed sidebar. */}
          <span className={collapsed ? "lg:sr-only" : undefined}>{destination.label}</span>
        </>
      )}
    </NavLink>
  );
}

const USERS_ICON = (
  <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <circle cx="9" cy="8" r="3.2" />
    <path d="M3.5 19a5.5 5.5 0 0 1 11 0" />
    <path d="M16 5.2a3.2 3.2 0 0 1 0 5.6" />
    <path d="M17 14.2A5.5 5.5 0 0 1 20.5 19" />
  </svg>
);

const MORE_ICON = (
  <svg viewBox="0 0 24 24" className="h-5 w-5" aria-hidden="true">
    <path
      fill="currentColor"
      d="M5.5 10.3a1.7 1.7 0 1 1 0 3.4 1.7 1.7 0 0 1 0-3.4Zm6.5 0a1.7 1.7 0 1 1 0 3.4 1.7 1.7 0 0 1 0-3.4Zm6.5 0a1.7 1.7 0 1 1 0 3.4 1.7 1.7 0 0 1 0-3.4Z"
    />
  </svg>
);

/** Bottom-bar "More" tab and its sheet; only rendered below `lg`, where the sidebar is a bottom bar. */
function MoreMenu({ destinations }: { destinations: NavDestination[] }) {
  const [open, setOpen] = useState(false);
  const { pathname } = useLocation();
  const holdsActivePage = destinations.some((d) => matchPath({ path: d.to, end: false }, pathname));
  const highlighted = open || holdsActivePage;

  if (destinations.length === 0) return null;
  return (
    <Popover.Root open={open} onOpenChange={setOpen} modal>
      {open ? <div className="fixed inset-0 z-[55] bg-slate-950/60 lg:hidden" aria-hidden="true" /> : null}
      <Popover.Trigger asChild>
        <button
          type="button"
          className={[
            "flex flex-col items-center justify-center gap-1 rounded-xl py-1.5 text-[12px] font-medium transition lg:hidden",
            highlighted ? "text-white" : "text-slate-400 hover:text-white",
          ].join(" ")}
        >
          <NavIcon icon={MORE_ICON} active={highlighted} />
          <span>More</span>
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          side="top"
          align="end"
          sideOffset={0}
          collisionPadding={0}
          // Let the sheet slide fully to the viewport edge instead of staying pinned to the tab.
          sticky="always"
          aria-label="More destinations"
          className="z-[70] w-screen rounded-t-2xl border-t border-night-500 bg-night-900 px-3 pb-3 pt-2 shadow-panel lg:hidden"
        >
          <div className="mx-auto mb-2 h-1 w-10 rounded-full bg-night-500" aria-hidden="true" />
          <nav aria-label="More" className="flex flex-col gap-1">
            {destinations.map((destination) => (
              <NavLink
                key={destination.to}
                to={destination.to}
                onClick={() => setOpen(false)}
                className={({ isActive }) =>
                  [
                    "flex min-h-12 items-center gap-3 rounded-xl px-3 text-sm transition",
                    isActive ? "bg-night-600 text-white" : "text-slate-200 hover:bg-night-700",
                  ].join(" ")
                }
              >
                <span className="text-slate-400">{destination.icon}</span>
                {destination.label}
              </NavLink>
            ))}
          </nav>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

export default function Sidebar({ collapsed, onToggle }: SidebarProps) {
  // The Users page manages org membership, gated by org-membership role
  // (Maintainer/Owner). useManageableOrgs() is the same tenant-aware signal
  // the Users and Integrations pages use to decide whether there is anything
  // the current user can manage.
  const manageableOrgs = useManageableOrgs();
  const canManageOrgUsers = (manageableOrgs.data?.length ?? 0) > 0;
  const destinations: NavDestination[] = [
    { to: getRoute("ui_dashboard_path"), label: "Dashboard", icon: ObjectIcons.dashboard, mobile: "primary" },
    { to: `${getRoute("ui_findings_path")}?active=true`, label: "Findings", icon: ObjectIcons.findings, mobile: "primary" },
    { to: getRoute("ui_products_path"), label: "Projects", icon: ObjectIcons.projects, mobile: "primary" },
    { to: getRoute("ui_pipelines_path"), label: "Pipelines", icon: ObjectIcons.pipelines, mobile: "primary" },
    { to: getRoute("ui_calendar_path"), label: "Calendar", icon: ObjectIcons.calendar, mobile: "more" },
    ...(canManageOrgUsers
      ? [
          { to: getRoute("ui_users_path"), label: "Users", icon: USERS_ICON, mobile: "more" as const },
          { to: getRoute("ui_org_integrations_path"), label: "Integrations", icon: ObjectIcons.integrations, mobile: "more" as const },
        ]
      : []),
    // On phones My Account is in the profile menu of the top bar.
    { to: getRoute("ui_settings_path"), label: "My Account", icon: ObjectIcons.settings, mobile: "none" },
  ];
  // A sheet holding a single destination is just an extra tap; give it the fifth slot instead.
  const singleMore = destinations.filter((d) => d.mobile === "more").length === 1;
  const placed = singleMore
    ? destinations.map((d): NavDestination => (d.mobile === "more" ? { ...d, mobile: "primary" } : d))
    : destinations;
  return (
    <aside
      className={[
        "z-[60] border-night-500 bg-night-900/95",
        "fixed bottom-0 left-0 right-0 flex h-16 items-center gap-3 border-t px-1",
        "lg:sticky lg:top-0 lg:h-screen lg:flex-col lg:border-r lg:border-t-0 lg:py-4 lg:transition-[width] lg:duration-200 lg:overflow-hidden lg:pr-2 box-border",
        collapsed ? "lg:w-[64px] lg:px-2" : "lg:w-[216px] lg:px-4",
      ].join(" ")}
    >
      <div className="hidden w-full flex-col lg:flex">
        <div
          className={[
            "flex items-center gap-3",
            collapsed ? "justify-center" : "justify-between",
          ].join(" ")}
        >
          <div className={collapsed ? "hidden" : "w-full pl-3"}>
            <div className="text-xs uppercase tracking-[0.3em] text-brand-500 leading-none">
              AIST Portal
            </div>
          </div>
          <button
            className="rounded-xl px-3 py-2 transition flex items-center justify-center gap-2 lg:w-10 lg:px-0 lg:py-3 lg:justify-center text-slate-400 hover:text-white hover:bg-night-700"
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-expanded={!collapsed}
            onClick={onToggle}
          >
            <svg
              viewBox="0 0 24 24"
              className={[
                "h-4 w-4 transition-transform",
                collapsed ? "rotate-180" : "",
              ].join(" ")}
              aria-hidden="true"
            >
              <path
                fill="currentColor"
                d="M15 6 9 12l6 6 1.4-1.4L11.8 12 16.4 7.4 15 6Z"
              />
            </svg>
          </button>
        </div>
        {collapsed ? null : (
          <div className="mt-1 pl-3 text-[11px] text-slate-500">Security Intelligence</div>
        )}
      </div>
      <div className={["hidden lg:block", collapsed ? "opacity-0" : "opacity-100"].join(" ")}>
        <div className="mt-2 h-px w-full bg-gradient-to-r from-transparent via-brand-500/40 to-transparent" />
      </div>
      <nav
        aria-label="Primary"
        className={[
          "grid flex-1 grid-cols-5 items-center gap-1 text-sm",
          "lg:flex lg:w-full lg:flex-col lg:items-stretch lg:justify-start lg:gap-2",
          collapsed ? "lg:items-center lg:mt-8" : "lg:mt-8",
        ].join(" ")}
      >
        {placed.map((destination) => (
          <NavItem key={destination.to} destination={destination} collapsed={collapsed} />
        ))}
        <MoreMenu destinations={placed.filter((d) => d.mobile === "more")} />
        <div className="mt-auto hidden lg:block" />
      </nav>
    </aside>
  );
}
