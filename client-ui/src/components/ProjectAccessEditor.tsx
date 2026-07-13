import { useMemo, useState } from "react";

import SelectField from "./SelectField";
import { ROLE_OPTIONS } from "../lib/orgMembers";
import { roleRank } from "../lib/permissions";

export type RoleByProject = Record<number, number | null>;

type ProjectOption = { id: number; name: string };

// A real (non-empty) sentinel — Radix's Select.Root treats an empty-string
// value as "nothing selected" and falls back to the placeholder, which would
// silently hide the "No access" label even though it's a meaningful,
// deliberately-chosen state here (not an unset field).
const NO_ACCESS = "none";
const ACCESS_OPTIONS = [
  { value: NO_ACCESS, label: "No access" },
  ...ROLE_OPTIONS.map((role) => ({ value: String(role.id), label: role.name })),
];

/**
 * Searchable project-access table: one role dropdown per project ("No access"
 * revokes), plus multi-select + bulk role apply. Reused by the invite flow and
 * the Manage-access drawer.
 */
export default function ProjectAccessEditor({
  projects,
  roleByProject,
  onSetRole,
  disabled = false,
  maxRoleId = null,
  emptyLabel = "This organization has no projects.",
}: {
  projects: ProjectOption[];
  roleByProject: RoleByProject;
  onSetRole: (projectId: number, roleId: number | null) => void | Promise<void>;
  disabled?: boolean;
  // A full member's per-project role is a downgrade-only override — the
  // backend rejects granting a role above their org-wide role (see
  // service.py's _grant_project). Passing that org role here greys out the
  // options above it instead of letting the user pick one that will 400.
  // null/undefined means no cap (restricted members are exempt on the
  // backend, and the invite flow has no org role yet to cap against).
  maxRoleId?: number | null;
  emptyLabel?: string;
}) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [bulkRole, setBulkRole] = useState<string>(NO_ACCESS);
  const [applying, setApplying] = useState(false);
  const rowsDisabled = disabled || applying;

  const accessOptions = useMemo(() => {
    if (maxRoleId == null) return ACCESS_OPTIONS;
    const cap = roleRank[maxRoleId] ?? -1;
    return ACCESS_OPTIONS.map((option) =>
      option.value === NO_ACCESS || (roleRank[Number(option.value)] ?? -1) <= cap
        ? option
        : { ...option, disabled: true },
    );
  }, [maxRoleId]);

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

  async function applyBulk() {
    const roleId = bulkRole === NO_ACCESS ? null : Number(bulkRole);
    const targets = Array.from(selected);
    setSelected(new Set());
    // Apply one at a time (not Promise.all/forEach) — a "select all" + bulk
    // apply on a large project list must not fire hundreds of concurrent
    // grant/revoke requests. `applying` blocks further input synchronously,
    // before the first request even starts.
    setApplying(true);
    try {
      for (const projectId of targets) {
        await onSetRole(projectId, roleId);
      }
    } finally {
      setApplying(false);
    }
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
          <input
            type="checkbox"
            className="accent-brand-500"
            checked={allVisibleSelected}
            onChange={toggleAllVisible}
            disabled={rowsDisabled}
          />
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
                className="accent-brand-500"
                checked={selected.has(project.id)}
                onChange={() => toggle(project.id)}
                disabled={rowsDisabled}
              />
              <span className="min-w-0 truncate text-sm text-slate-100">{project.name}</span>
              <SelectField
                label={`Access for ${project.name}`}
                hideLabel
                value={current === null ? NO_ACCESS : String(current)}
                disabled={rowsDisabled}
                options={accessOptions}
                onChange={(value) => onSetRole(project.id, value === NO_ACCESS ? null : Number(value))}
              />
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
          <div className="w-40">
            <SelectField
              label="Bulk role"
              hideLabel
              value={bulkRole}
              disabled={rowsDisabled}
              options={accessOptions}
              onChange={setBulkRole}
            />
          </div>
          <button type="button" className="aist-icon-button" disabled={rowsDisabled} onClick={applyBulk}>
            Apply to selected
          </button>
        </div>
      ) : null}
    </div>
  );
}
