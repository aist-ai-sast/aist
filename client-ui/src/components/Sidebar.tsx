import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { getRoute } from "../lib/routes";
import PermissionGate from "./PermissionGate";
import { ObjectIcons } from "./ObjectIcons";

type SidebarProps = {
  collapsed: boolean;
  onToggle: () => void;
};

const NAV_LINK_CLASS = (collapsed: boolean) =>
  ({ isActive }: { isActive: boolean }) =>
    [
      "rounded-xl px-3 py-2 transition flex items-center justify-center gap-2",
      collapsed
        ? "lg:w-10 lg:px-0 lg:py-3 lg:justify-center"
        : "lg:px-3 lg:py-3 lg:justify-start lg:text-left lg:w-full",
      isActive
        ? "bg-night-600 text-white"
        : "text-slate-400 hover:text-white hover:bg-night-700",
    ].join(" ");

function NavItem({ to, label, icon, collapsed }: { to: string; label: string; icon: ReactNode; collapsed: boolean }) {
  return (
    <NavLink to={to} className={NAV_LINK_CLASS(collapsed)}>
      <span className="text-slate-400">{icon}</span>
      {collapsed ? null : <span className="hidden lg:inline">{label}</span>}
    </NavLink>
  );
}

export default function Sidebar({ collapsed, onToggle }: SidebarProps) {
  const baseLinks = [
    { to: getRoute("ui_dashboard_path"), label: "Dashboard", icon: ObjectIcons.dashboard },
    { to: `${getRoute("ui_findings_path")}?active=true`, label: "Findings", icon: ObjectIcons.findings },
    { to: getRoute("ui_products_path"), label: "Projects", icon: ObjectIcons.projects },
    { to: getRoute("ui_pipelines_path"), label: "Pipelines", icon: ObjectIcons.pipelines },
    { to: getRoute("ui_calendar_path"), label: "Calendar", icon: ObjectIcons.calendar },
  ];
  const accountLinks = [
    { to: getRoute("ui_settings_path"), label: "My Account", icon: ObjectIcons.settings },
  ];
  return (
    <aside
      className={[
        "z-[60] border-night-500 bg-night-900/95",
        "fixed bottom-0 left-0 right-0 flex h-16 items-center gap-3 border-t px-4",
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
        className={[
          "flex flex-1 items-center justify-around gap-2 text-sm",
          "lg:w-full lg:flex-col lg:items-stretch lg:justify-start lg:gap-2",
          collapsed ? "lg:items-center lg:mt-8" : "lg:mt-8",
        ].join(" ")}
      >
        {baseLinks.map((link) => (
          <NavItem key={link.to} to={link.to} label={link.label} icon={link.icon} collapsed={collapsed} />
        ))}
        <PermissionGate action="manage_access">
          <NavItem
            to={getRoute("ui_users_path")}
            label="Users"
            icon={(
              <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <circle cx="9" cy="8" r="3.2" />
                <path d="M3.5 19a5.5 5.5 0 0 1 11 0" />
                <path d="M16 5.2a3.2 3.2 0 0 1 0 5.6" />
                <path d="M17 14.2A5.5 5.5 0 0 1 20.5 19" />
              </svg>
            )}
            collapsed={collapsed}
          />
          <NavItem
            to={getRoute("ui_org_integrations_path")}
            label="Integrations"
            icon={ObjectIcons.integrations}
            collapsed={collapsed}
          />
        </PermissionGate>
        {accountLinks.map((link) => (
          <NavItem key={link.to} to={link.to} label={link.label} icon={link.icon} collapsed={collapsed} />
        ))}
        <div className="mt-auto" />
      </nav>
    </aside>
  );
}
