import { useState } from "react";
import * as Select from "@radix-ui/react-select";

import { useCreateWorkItem, useDeleteWorkItem, useUpdateWorkItem } from "../lib/mutations";
import { useWorkItems } from "../lib/queries";
import { useToast } from "./ToastProvider";
import TextInput from "./TextInput";
import type { WorkItemStatusCategory } from "../types";

type WorkItemsPanelProps = {
  findingId: number;
};

const STATUS_LABEL: Record<WorkItemStatusCategory, string> = {
  OPEN: "Open",
  IN_PROGRESS: "In Progress",
  DONE: "Done",
  CANCELLED: "Cancelled",
  UNKNOWN: "Unknown",
};

function statusBadgeClass(status: WorkItemStatusCategory): string {
  switch (status) {
    case "DONE": return "border-emerald-500/40 bg-emerald-500/10 text-emerald-300";
    case "CANCELLED": return "border-slate-500/40 bg-slate-500/10 text-slate-400";
    case "IN_PROGRESS": return "border-brand-500/40 bg-brand-500/10 text-brand-300";
    case "OPEN": return "border-amber-400/40 bg-amber-400/10 text-amber-300";
    default: return "border-night-500 bg-night-800 text-slate-400";
  }
}

const btnBase =
  "inline-flex items-center justify-center rounded-xl border px-3 py-1.5 text-xs font-medium transition disabled:opacity-50 disabled:cursor-not-allowed";
const btnPrimary =
  `${btnBase} border-brand-500/50 bg-brand-500/10 text-brand-200 hover:bg-brand-500/20`;
const btnDefault =
  `${btnBase} border-night-500 bg-night-800 text-slate-300 hover:border-night-400`;

const STATUS_OPTIONS: WorkItemStatusCategory[] = ["OPEN", "IN_PROGRESS", "DONE", "CANCELLED", "UNKNOWN"];

function StatusSelect({
  value,
  onChange,
  disabled,
}: {
  value: WorkItemStatusCategory;
  onChange: (v: WorkItemStatusCategory) => void;
  disabled?: boolean;
}) {
  return (
    <Select.Root value={value} onValueChange={(v) => onChange(v as WorkItemStatusCategory)} disabled={disabled}>
      <Select.Trigger
        className={[
          "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide outline-none transition",
          "cursor-pointer hover:opacity-80 data-[state=open]:opacity-80",
          statusBadgeClass(value),
        ].join(" ")}
        onClick={(e) => e.stopPropagation()}
      >
        <Select.Value />
        <Select.Icon>
          <svg viewBox="0 0 20 20" className="h-2.5 w-2.5 shrink-0" fill="currentColor" aria-hidden="true">
            <path d="M5.25 7.5 10 12.25 14.75 7.5H5.25Z" />
          </svg>
        </Select.Icon>
      </Select.Trigger>
      <Select.Portal>
        <Select.Content
          position="popper"
          className="z-50 overflow-hidden rounded-xl border border-night-500 bg-night-900 shadow-panel"
        >
          <Select.Viewport className="p-1">
            {STATUS_OPTIONS.map((s) => (
              <Select.Item
                key={s}
                value={s}
                className="cursor-pointer select-none rounded-lg px-3 py-1.5 text-xs text-slate-200 outline-none data-[highlighted]:bg-night-700 data-[state=checked]:bg-night-600"
              >
                <Select.ItemText>{STATUS_LABEL[s]}</Select.ItemText>
              </Select.Item>
            ))}
          </Select.Viewport>
        </Select.Content>
      </Select.Portal>
    </Select.Root>
  );
}

export default function WorkItemsPanel({ findingId }: WorkItemsPanelProps) {
  const toast = useToast();
  const { data: workItems = [], isLoading } = useWorkItems(findingId);
  const createMutation = useCreateWorkItem(findingId);
  const deleteMutation = useDeleteWorkItem(findingId);
  const updateMutation = useUpdateWorkItem(findingId);

  const [addOpen, setAddOpen] = useState(false);
  const [newUrl, setNewUrl] = useState("");
  const [newKey, setNewKey] = useState("");
  const [newTitle, setNewTitle] = useState("");

  const handleAdd = () => {
    if (!newUrl.trim()) return;
    createMutation.mutate(
      {
        external_url: newUrl.trim(),
        external_key: newKey.trim() || undefined,
        title: newTitle.trim() || undefined,
      },
      {
        onSuccess: () => {
          toast.push("Work item linked.", "success");
          setAddOpen(false);
          setNewUrl("");
          setNewKey("");
          setNewTitle("");
        },
        onError: (err) => {
          toast.push(
            `Failed to link: ${err instanceof Error ? err.message : String(err)}`,
            "error",
          );
        },
      },
    );
  };

  const handleDelete = (linkId: number) => {
    deleteMutation.mutate(linkId, {
      onSuccess: () => toast.push("Work item removed.", "success"),
      onError: (err) =>
        toast.push(`Failed: ${err instanceof Error ? err.message : String(err)}`, "error"),
    });
  };

  const handleStatusChange = (linkId: number, status_category: string) => {
    updateMutation.mutate(
      { linkId, status_category },
      {
        onError: (err) =>
          toast.push(`Failed: ${err instanceof Error ? err.message : String(err)}`, "error"),
      },
    );
  };

  return (
    <div className="space-y-3">
      {isLoading ? (
        <div className="text-xs text-slate-400">Loading…</div>
      ) : workItems.length === 0 ? (
        <div className="text-xs text-slate-400">No linked work items.</div>
      ) : (
        <ul className="divide-y divide-night-500/40 rounded-xl border border-night-500/40 bg-night-800/30 px-3">
          {workItems.map((wi) => (
            <li
              key={wi.id}
              className="flex items-start justify-between gap-3 py-2 text-xs"
            >
              <div className="min-w-0 flex-1 space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  {wi.externalKey ? (
                    <span className="font-mono text-slate-300">{wi.externalKey}</span>
                  ) : null}
                  {wi.provider === null ? (
                    <StatusSelect
                      value={wi.statusCategory}
                      onChange={(v) => handleStatusChange(wi.id, v)}
                      disabled={updateMutation.isPending}
                    />
                  ) : (
                    <span
                      className={[
                        "rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
                        statusBadgeClass(wi.statusCategory),
                      ].join(" ")}
                    >
                      {STATUS_LABEL[wi.statusCategory]}
                    </span>
                  )}
                  {wi.providerName ? (
                    <span className="text-slate-500">{wi.providerName}</span>
                  ) : null}
                </div>
                {wi.title ? (
                  <div className="text-slate-300 line-clamp-1">{wi.title}</div>
                ) : null}
                <a
                  href={wi.externalUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="block truncate text-brand-400 hover:underline"
                >
                  {wi.externalUrl}
                </a>
              </div>
              <button
                type="button"
                className="aist-icon-button shrink-0 text-slate-400 hover:text-danger-400"
                onClick={() => handleDelete(wi.id)}
                disabled={deleteMutation.isPending}
                aria-label="Remove work item"
              >
                <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
                  <path fill="currentColor" d="M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12ZM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4Z" />
                </svg>
              </button>
            </li>
          ))}
        </ul>
      )}

      {addOpen ? (
        <div className="rounded-lg border border-night-500 bg-night-800/60 p-3 space-y-2">
          <div className="text-xs uppercase tracking-[0.15em] text-slate-400">Link external issue</div>
          <TextInput
            variant="compact"
            type="url"
            placeholder="Issue URL (required)"
            value={newUrl}
            onChange={(e) => setNewUrl(e.target.value)}
          />
          <div className="grid grid-cols-2 gap-2">
            <TextInput
              variant="compact"
              type="text"
              placeholder="Key (e.g. PROJ-42)"
              value={newKey}
              onChange={(e) => setNewKey(e.target.value)}
            />
            <TextInput
              variant="compact"
              type="text"
              placeholder="Title (optional)"
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
            />
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              className={btnPrimary}
              onClick={handleAdd}
              disabled={!newUrl.trim() || createMutation.isPending}
            >
              {createMutation.isPending ? "Adding…" : "Add"}
            </button>
            <button
              type="button"
              className={btnDefault}
              onClick={() => {
                setAddOpen(false);
                setNewUrl("");
                setNewKey("");
                setNewTitle("");
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          className={btnDefault}
          onClick={() => setAddOpen(true)}
        >
          + Link issue
        </button>
      )}
    </div>
  );
}
