import { useMemo, useState } from "react";

import { ROLE_OPTIONS } from "../lib/orgMembers";

export type RoleByProject = Record<number, number | null>;

type ProjectOption = { id: number; name: string };

const NO_ACCESS = "";
const ACCESS_OPTIONS = [
  { value: NO_ACCESS, label: "No access" },
  ...ROLE_OPTIONS.map((role) => ({ value: String(role.id), label: role.name })),
];

const ROLE_SELECT_CLASS =
  "h-9 rounded-lg border border-night-500 bg-night-600 px-2 text-xs text-white outline-none " +
  "focus:border-brand-600 focus:ring-1 focus:ring-brand-600/60 disabled:opacity-60";

/**
 * Searchable project-access table: one role dropdown per project ("No access"
 * revokes), plus multi-select + bulk role apply. Reused by the invite flow and
 * the Manage-access drawer. Uses a native <select> so it renders reliably inside
 * overlays/drawers.
 */
export default function ProjectAccessEditor({
  projects,
  roleByProject,
  onSetRole,
  disabled = false,
  emptyLabel = "This organization has no projects.",
}: {
  projects: ProjectOption[];
  roleByProject: RoleByProject;
  onSetRole: (projectId: number, roleId: number | null) => void;
  disabled?: boolean;
  emptyLabel?: string;
}) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [bulkRole, setBulkRole] = useState<string>(NO_ACCESS);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return projects;
    return projects.filter((project) => project.name.toLowerCase().includes(needle));
  }, [projects, query]);

  const allVisibleSelected = filtered.length > 0 && filtered.every((p) => selected.has(p.id));

  function toggle(projectId: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(projectId)) next.delete(projectId);
      else next.add(projectId);
      return next;
    });
  }

  function toggleAllVisible() {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allVisibleSelected) filtered.forEach((p) => next.delete(p.id));
      else filtered.forEach((p) => next.add(p.id));
      return next;
    });
  }

  function applyBulk() {
    const roleId = bulkRole === NO_ACCESS ? null : Number(bulkRole);
    selected.forEach((projectId) => onSetRole(projectId, roleId));
    setSelected(new Set());
  }

  if (!projects.length) {
    return <p className="text-xs text-slate-400">{emptyLabel}</p>;
  }

  return (
    <div className="space-y-3">
      <input
        type="text"
        className="h-9 w-full rounded-lg border border-night-500 bg-night-800 px-3 text-xs text-slate-200 outline-none focus:border-brand-600"
        placeholder="Search projects..."
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />

      <div className="max-h-80 overflow-y-auto rounded-xl border border-night-500/70">
        <div className="grid grid-cols-[2rem_1fr_9rem] items-center gap-2 border-b border-night-500/70 px-3 py-2 text-[11px] uppercase tracking-wide text-slate-400">
          <input type="checkbox" checked={allVisibleSelected} onChange={toggleAllVisible} disabled={disabled} />
          <span>Project</span>
          <span>Access</span>
        </div>
        {filtered.map((project) => {
          const current = roleByProject[project.id] ?? null;
          return (
            <div
              key={project.id}
              className="grid grid-cols-[2rem_1fr_9rem] items-center gap-2 border-b border-night-500/40 px-3 py-2 last:border-0"
            >
              <input
                type="checkbox"
                checked={selected.has(project.id)}
                onChange={() => toggle(project.id)}
                disabled={disabled}
              />
              <span className="min-w-0 truncate text-sm text-slate-100">{project.name}</span>
              <select
                className={ROLE_SELECT_CLASS}
                value={current === null ? NO_ACCESS : String(current)}
                disabled={disabled}
                onChange={(event) => onSetRole(project.id, event.target.value === NO_ACCESS ? null : Number(event.target.value))}
              >
                {ACCESS_OPTIONS.map((option) => (
                  <option key={option.value || "none"} value={option.value}>{option.label}</option>
                ))}
              </select>
            </div>
          );
        })}
        {filtered.length === 0 ? (
          <div className="px-3 py-4 text-xs text-slate-400">No projects match “{query}”.</div>
        ) : null}
      </div>

      {selected.size > 0 ? (
        <div className="flex items-center gap-2 rounded-xl border border-night-500/70 bg-night-800/60 px-3 py-2">
          <span className="text-xs text-slate-300">{selected.size} selected</span>
          <select
            className={ROLE_SELECT_CLASS}
            value={bulkRole}
            disabled={disabled}
            onChange={(event) => setBulkRole(event.target.value)}
          >
            {ACCESS_OPTIONS.map((option) => (
              <option key={option.value || "none"} value={option.value}>{option.label}</option>
            ))}
          </select>
          <button type="button" className="aist-icon-button" disabled={disabled} onClick={applyBulk}>
            Apply to selected
          </button>
        </div>
      ) : null}
    </div>
  );
}
