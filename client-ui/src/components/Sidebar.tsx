import { NavLink } from "react-router-dom";

const Icons = {
  findings: (
    <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
      <path
        fill="currentColor"
        d="M7 3h7l5 5v13a1 1 0 0 1-1 1H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm6 1.5V9h4.5L13 4.5ZM8 12h8v1.5H8V12Zm0 4h8v1.5H8V16Z"
      />
    </svg>
  ),
  products: (
    <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
      <path
        fill="currentColor"
        d="M4 7.5 12 3l8 4.5-8 4.5-8-4.5Zm0 3.5 8 4.5 8-4.5V18l-8 4-8-4v-7Z"
      />
    </svg>
  ),
  pipelines: (
    <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
      <path
        fill="currentColor"
        d="M5 4h5v5H5V4Zm9 0h5v5h-5V4ZM5 15h5v5H5v-5Zm9 0h5v5h-5v-5ZM7.5 9.5h9v5h-9v-5Z"
      />
    </svg>
  ),
  search: (
    <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
      <path
        fill="currentColor"
        d="M10.5 3a7.5 7.5 0 1 1-4.59 13.43l-2.7 2.7-1.06-1.06 2.7-2.7A7.5 7.5 0 0 1 10.5 3Zm0 1.5a6 6 0 1 0 0 12a6 6 0 0 0 0-12Z"
      />
    </svg>
  ),
  settings: (
    <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
      <path
        fill="currentColor"
        d="M12 7a5 5 0 1 1 0 10a5 5 0 0 1 0-10Zm9 5a7.77 7.77 0 0 0-.1-1l2.02-1.57-2-3.46-2.42.7a8.6 8.6 0 0 0-1.7-1l-.32-2.5H9.5l-.32 2.5a8.6 8.6 0 0 0-1.7 1l-2.42-.7-2 3.46L4.1 11a7.77 7.77 0 0 0 0 2l-2.02 1.57 2 3.46 2.42-.7c.53.4 1.1.73 1.7 1l.32 2.5h5.96l.32-2.5c.6-.27 1.17-.6 1.7-1l2.42.7 2-3.46L20.9 13c.07-.33.1-.66.1-1Z"
      />
    </svg>
  ),
};

const links = [
  { to: "/", label: "Findings", icon: Icons.findings },
  { to: "/products", label: "Products", icon: Icons.products },
  { to: "/pipelines", label: "Pipelines", icon: Icons.pipelines },
  { to: "/search", label: "Search", icon: Icons.search },
  { to: "/settings", label: "Settings", icon: Icons.settings },
];

export default function Sidebar() {
  return (
    <aside className="hidden lg:flex lg:flex-col lg:w-64 border-r border-night-500 bg-night-900/90 px-6 py-6">
      <div className="text-brand-500 text-xs tracking-[0.2em] font-semibold uppercase mb-8">
        AIST Client
      </div>
      <nav className="flex flex-col gap-2 text-sm">
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
            <span>{link.label}</span>
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}
