import { NavLink } from "react-router-dom";

const links = [
  { to: "/", label: "Findings" },
  { to: "/products", label: "Products" },
  { to: "/pipelines", label: "Pipelines" },
  { to: "/search", label: "Search" },
  { to: "/settings", label: "Settings" },
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
                "rounded-xl px-4 py-3 transition",
                isActive
                  ? "bg-night-600 text-white"
                  : "text-slate-400 hover:text-white hover:bg-night-700",
              ].join(" ")
            }
          >
            {link.label}
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}
