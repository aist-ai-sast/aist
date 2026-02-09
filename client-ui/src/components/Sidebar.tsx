import { NavLink } from "react-router-dom";
import { logoutSession } from "../lib/auth";
import { useToast } from "./ToastProvider";

const Icons = {
  findings: (
    <svg viewBox="0 0 24 24" className="h-5 w-5" aria-hidden="true">
      <path
        fill="currentColor"
        d="M7 3h7l5 5v13a1 1 0 0 1-1 1H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm6 1.5V9h4.5L13 4.5ZM8 12h8v1.5H8V12Zm0 4h8v1.5H8V16Z"
      />
    </svg>
  ),
  products: (
    <svg viewBox="0 0 24 24" className="h-5 w-5" aria-hidden="true">
      <path
        fill="currentColor"
        d="M4 7.5 12 3l8 4.5-8 4.5-8-4.5Zm0 3.5 8 4.5 8-4.5V18l-8 4-8-4v-7Z"
      />
    </svg>
  ),
  pipelines: (
    <svg viewBox="0 0 24 24" className="h-5 w-5" aria-hidden="true">
      <path
        fill="currentColor"
        d="M5 4h5v5H5V4Zm9 0h5v5h-5V4ZM5 15h5v5H5v-5Zm9 0h5v5h-5v-5ZM7.5 9.5h9v5h-9v-5Z"
      />
    </svg>
  ),
  settings: (
    <svg viewBox="0 0 24 24" className="h-5 w-5" aria-hidden="true">
      <path
        fill="currentColor"
        d="M19.14 12.94c.04-.31.06-.63.06-.94s-.02-.63-.06-.94l2.03-1.58a.75.75 0 0 0 .18-.96l-1.92-3.32a.75.75 0 0 0-.92-.33l-2.39.96a7.06 7.06 0 0 0-1.62-.94l-.36-2.54a.75.75 0 0 0-.74-.64h-3.84a.75.75 0 0 0-.74.64l-.36 2.54c-.57.23-1.12.54-1.62.94l-2.39-.96a.75.75 0 0 0-.92.33L2.65 7.52a.75.75 0 0 0 .18.96l2.03 1.58c-.04.31-.06.63-.06.94s.02.63.06.94l-2.03 1.58a.75.75 0 0 0-.18.96l1.92 3.32c.2.35.62.5.98.35l2.39-.96c.5.4 1.05.71 1.62.94l.36 2.54c.06.37.37.64.74.64h3.84c.37 0 .68-.27.74-.64l.36-2.54c.57-.23 1.12-.54 1.62-.94l2.39.96c.36.15.78 0 .98-.35l1.92-3.32a.75.75 0 0 0-.18-.96l-2.03-1.58ZM12 15.5a3.5 3.5 0 1 1 0-7a3.5 3.5 0 0 1 0 7Z"
      />
    </svg>
  ),
};

const links = [
  { to: "/", label: "Findings", icon: Icons.findings },
  { to: "/products", label: "Products", icon: Icons.products },
  { to: "/pipelines", label: "Pipelines", icon: Icons.pipelines },
  { to: "/settings", label: "Settings", icon: Icons.settings },
];

type SidebarProps = {
  collapsed: boolean;
  onToggle: () => void;
};

export default function Sidebar({ collapsed, onToggle }: SidebarProps) {
  const toast = useToast();
  return (
    <aside
      className={[
        "sticky top-0 z-50 flex h-screen flex-col border-r border-night-500 bg-night-900/90 py-6 transition-[width] duration-200",
        collapsed ? "w-20 px-3" : "w-64 px-6",
      ].join(" ")}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-11 w-11 items-center justify-center rounded-xl border border-night-500 bg-night-700">
            <img src="/logo.svg" alt="AIST" className="h-8 w-8 object-contain" />
          </div>
          {collapsed ? null : (
            <div className="text-brand-500 text-xs tracking-[0.2em] font-semibold uppercase">
              AIST Client
            </div>
          )}
        </div>
        <button
          className="rounded-lg border border-night-500 bg-night-700 p-2 text-slate-300 relative z-50"
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-expanded={!collapsed}
          onClick={onToggle}
        >
          <svg
            viewBox="0 0 24 24"
            className={[
              "h-5 w-5 transition-transform",
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
      <nav className={["mt-8 flex flex-col gap-2 text-sm", collapsed ? "items-center" : ""].join(" ")}>
        {links.map((link) => (
          <NavLink
            key={link.to}
            to={link.to}
            className={({ isActive }) =>
              [
                "rounded-xl px-4 py-3 transition flex items-center gap-3",
                isActive
                  ? "bg-night-600 text-white"
                  : "text-slate-400 hover:text-white hover:bg-night-700",
              ].join(" ")
            }
          >
            <span className="text-slate-400">{link.icon}</span>
            {collapsed ? null : <span>{link.label}</span>}
          </NavLink>
        ))}
      </nav>
      <div className="mt-auto pt-6">
        <button
          className={[
            "rounded-xl border border-night-500 bg-night-700 px-4 py-3 text-xs text-slate-200 inline-flex items-center justify-center gap-2",
            collapsed ? "w-12 px-0" : "w-full",
          ].join(" ")}
          onClick={async () => {
            await logoutSession();
            toast.push("Signed out.", "success");
            window.location.reload();
          }}
          title={collapsed ? "Sign out" : undefined}
        >
          <svg viewBox="0 0 24 24" className="h-5 w-5" aria-hidden="true">
            <path
              fill="currentColor"
              d="M10 5H5v14h5v2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5v2Zm4.5 2.5L19 12l-4.5 4.5-1.4-1.4L15.2 13H9v-2h6.2l-2.1-2.1 1.4-1.4Z"
            />
          </svg>
          {collapsed ? null : "Sign out"}
        </button>
      </div>
    </aside>
  );
}
