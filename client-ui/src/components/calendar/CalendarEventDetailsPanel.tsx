import type { CalendarEvent } from "../../types";
import type { ReactNode } from "react";
import {
  buildCalendarActionLinks,
  eventTypeLabel,
  formatCalendarDateTime,
  formatCalendarSummary,
  formatDurationFromSeconds,
  getSeverityDistribution,
  pipelineStatusBadge,
} from "../../lib/calendarEventUi";
import { severityBarClass } from "../../lib/badgeStyles";

type Props = {
  selectedEvent: CalendarEvent | null;
  selectedProjectId?: number;
  isLoading?: boolean;
  onNavigate: (path: string) => void;
};

function DetailCard({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`rounded-xl border border-night-500 bg-night-900/70 p-3 ${className}`}>{children}</div>;
}

export default function CalendarEventDetailsPanel({ selectedEvent, selectedProjectId, isLoading = false, onNavigate }: Props) {
  if (isLoading) {
    return (
      <div className="mt-4 space-y-3 animate-pulse">
        <div className="rounded-xl border border-night-500 bg-night-800/80 p-4">
          <div className="h-4 w-2/3 rounded bg-night-600" />
          <div className="mt-2 h-3 w-1/3 rounded bg-night-600" />
          <div className="mt-4 h-3 w-1/2 rounded bg-night-600" />
        </div>
        <div className="rounded-xl border border-night-500 bg-night-900/70 p-3">
          <div className="h-3 w-1/4 rounded bg-night-600" />
          <div className="mt-3 h-2 w-full rounded bg-night-700" />
          <div className="mt-2 h-2 w-5/6 rounded bg-night-700" />
        </div>
      </div>
    );
  }

  if (!selectedEvent) {
    return (
      <div className="mt-4 rounded-xl border border-night-500 bg-night-800/80 p-4 text-sm text-slate-400">
        Click any event to open a detailed panel.
      </div>
    );
  }

  const summary = selectedEvent.summary as {
    status?: string;
    duration_seconds?: number;
    branch?: string;
    commit?: string;
    findings?: number;
    severity?: Record<string, number>;
    actions?: Array<{ type?: string | null; status?: string | null }>;
  };
  const actions = buildCalendarActionLinks(selectedEvent, selectedProjectId);
  const primaryAction = actions[0] ?? null;
  const secondaryActions = actions.slice(1);
  const { rows: severityRows, total: severityTotal } = getSeverityDistribution(selectedEvent.summary);
  const showSeverityDistribution =
    (selectedEvent.eventType === "finding_created" || selectedEvent.eventType === "finding_mitigated")
    && selectedEvent.isAggregated
    && severityRows.length > 0;

  return (
    <div className="mt-3 space-y-3">
      <div className="aist-calendar-detail-hero rounded-xl border p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-slate-100 break-words">{selectedEvent.title}</h2>
            <div className="mt-1 text-xs text-slate-400">{eventTypeLabel(selectedEvent.eventType)}</div>
          </div>
          {selectedEvent.eventType === "pipeline_started" ? (
            <span
              className={[
                "rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-wide",
                pipelineStatusBadge(String(summary.status ?? "unknown")),
              ].join(" ")}
            >
              {String(summary.status ?? "unknown")}
            </span>
          ) : null}
        </div>
        <div className="mt-3 flex flex-wrap gap-3 text-xs text-slate-300">
          <span>{formatCalendarDateTime(selectedEvent.start, selectedEvent.isAllDay)}</span>
          {selectedEvent.eventType === "pipeline_started" ? (
            <span>Duration: {formatDurationFromSeconds(Number(summary.duration_seconds ?? 0)) ?? "—"}</span>
          ) : null}
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <span className="rounded-full border border-night-500 bg-night-800/70 px-2.5 py-1 text-[11px] text-slate-300">
            {selectedEvent.isFuture ? "Future event" : "Past event"}
          </span>
          {selectedEvent.isAggregated ? (
            <span className="rounded-full border border-night-500 bg-night-800/70 px-2.5 py-1 text-[11px] text-slate-300">
              Aggregated: {selectedEvent.count}
            </span>
          ) : null}
        </div>
      </div>

      {selectedEvent.eventType === "pipeline_started" ? (
        <DetailCard className="text-xs text-slate-300">
          <div className="grid grid-cols-2 gap-2">
            <div className="text-slate-400">Branch</div>
            <div className="text-slate-100">{String(summary.branch ?? "—")}</div>
            <div className="text-slate-400">Commit</div>
            <div className="text-slate-100 font-mono">{String(summary.commit ?? "—")}</div>
            <div className="text-slate-400">Findings</div>
            <div className="text-slate-100">{String(summary.findings ?? 0)}</div>
          </div>
          <div className="mt-3">
            <div className="mb-1 text-[11px] uppercase tracking-[0.12em] text-slate-400">Actions</div>
            <div className="flex flex-wrap gap-2">
              {(summary.actions ?? []).slice(0, 3).map((action, idx) => (
                <span
                  key={`action-${idx}-${action.type ?? "action"}`}
                  className="rounded-full border border-night-500 bg-night-800 px-2.5 py-1 text-[11px] text-slate-200"
                >
                  {action.type ?? "Action"} · {action.status ?? "pending"}
                </span>
              ))}
            </div>
          </div>
        </DetailCard>
      ) : (
        <>
          {showSeverityDistribution ? null : (
            <DetailCard className="text-sm text-slate-300">{formatCalendarSummary(selectedEvent.summary)}</DetailCard>
          )}
          {showSeverityDistribution ? (
            <DetailCard className="text-xs text-slate-200">
              <div className="mb-2 text-[11px] uppercase tracking-[0.14em] text-slate-400">Severity distribution</div>
              <div className="space-y-2">
                {severityRows.map((row) => {
                  const width = severityTotal > 0 ? Math.max(Math.round((row.count / severityTotal) * 100), 6) : 0;
                  return (
                    <div key={row.level} className="grid grid-cols-[70px_1fr_34px] items-center gap-2">
                      <div className="text-[11px] text-slate-300">{row.level}</div>
                      <div className="h-2 overflow-hidden rounded-full bg-night-700">
                        <div className={`h-full rounded-full ${severityBarClass(row.level)}`} style={{ width: `${width}%` }} />
                      </div>
                      <div className="text-right text-[11px] text-slate-300">{row.count}</div>
                    </div>
                  );
                })}
              </div>
            </DetailCard>
          ) : null}
        </>
      )}

      {actions.length ? (
        <div className="mt-1 rounded-lg border border-night-500/80 bg-night-800/70 px-2.5 py-1.5">
          <div className="flex flex-wrap gap-1.5">
            {primaryAction ? (
              <button
                type="button"
                className="rounded-md border border-brand-500/60 bg-brand-500/15 px-2.5 py-1 text-[11px] font-medium text-brand-50 hover:border-brand-400/70 hover:bg-brand-500/25"
                onClick={() => onNavigate(primaryAction.to)}
              >
                {primaryAction.label}
              </button>
            ) : null}
            {secondaryActions.map((action) => (
              <button
                key={action.to}
                type="button"
                className="rounded-md border border-night-500 bg-night-700 px-2.5 py-1 text-[11px] text-slate-100 hover:border-brand-500/60"
                onClick={() => onNavigate(action.to)}
              >
                {action.label}
              </button>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
