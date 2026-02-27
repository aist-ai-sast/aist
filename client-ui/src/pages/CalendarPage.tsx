import FullCalendar from "@fullcalendar/react";
import dayGridPlugin from "@fullcalendar/daygrid";
import interactionPlugin from "@fullcalendar/interaction";
import timeGridPlugin from "@fullcalendar/timegrid";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import CalendarEventDetailsPanel from "../components/calendar/CalendarEventDetailsPanel";
import CalendarEventTypeIcon from "../components/calendar/CalendarEventTypeIcon";
import PageErrorState from "../components/PageErrorState";
import SelectField from "../components/SelectField";
import { calendarViewToApiView, mapCalendarEventToUi } from "../lib/calendar";
import { formatCalendarSummary } from "../lib/calendarEventUi";
import { useCalendarEventDetail, useCalendarEvents, useProjects } from "../lib/queries";
import type { CalendarEvent, CalendarEventType, CalendarView } from "../types";

const EVENT_TYPE_META = [
  {
    id: "pipeline_finished",
    value: "pipeline_started",
    label: "Pipeline finished",
    colorClass: "fc-aist-pipeline-status-finished",
    pipelineStatus: "FINISHED",
  },
  {
    id: "pipeline_warnings",
    value: "pipeline_started",
    label: "Pipeline warnings",
    colorClass: "fc-aist-pipeline-status-warning",
    pipelineStatus: "FINISHED_WITH_WARNINGS",
  },
  {
    id: "pipeline_scheduled",
    value: "pipeline_scheduled",
    label: "Pipeline scheduled",
    colorClass: "fc-aist-type-pipeline_scheduled",
  },
  {
    id: "finding_created",
    value: "finding_created",
    label: "Finding created",
    colorClass: "fc-aist-type-finding_created",
  },
  {
    id: "finding_mitigated",
    value: "finding_mitigated",
    label: "Finding mitigated",
    colorClass: "fc-aist-type-finding_mitigated",
  },
  {
    id: "project_created",
    value: "project_created",
    label: "Project created",
    colorClass: "fc-aist-type-project_created",
  },
] as const;

const ALL_EVENT_TYPES = Array.from(new Set(EVENT_TYPE_META.map((item) => item.value))) as CalendarEventType[];

type EventTypeMeta = (typeof EVENT_TYPE_META)[number];

function EventTypeSelector({
  selected,
  onToggle,
  className = "",
}: {
  selected: string[];
  onToggle: (id: string) => void;
  className?: string;
}) {
  return (
    <div className={["flex flex-wrap gap-2", className].join(" ").trim()}>
      {EVENT_TYPE_META.map((item) => {
        const isActive = selected.includes(item.id);
        return (
          <button
            key={item.id}
            type="button"
            aria-pressed={isActive}
            onClick={() => onToggle(item.id)}
            className={[
              "inline-flex items-center gap-2 rounded-full border px-3 py-1 text-[11px] font-medium transition",
              isActive
                ? "border-brand-500 bg-brand-500/15 text-white"
                : "border-night-500 bg-night-800 text-slate-300 hover:border-night-400",
            ].join(" ")}
          >
            <span className={`h-2.5 w-2.5 rounded-full border border-night-500 ${item.colorClass}`} />
            <span>{item.label}</span>
          </button>
        );
      })}
    </div>
  );
}

function parseCalendarView(raw: string | null): CalendarView {
  if (raw === "day" || raw === "week" || raw === "month") return raw;
  return "month";
}

function toFullCalendarView(view: CalendarView): "timeGridDay" | "timeGridWeek" | "dayGridMonth" {
  if (view === "day") return "timeGridDay";
  if (view === "week") return "timeGridWeek";
  return "dayGridMonth";
}

function shortDate(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function parseMetaIds(raw: string | null): string[] {
  if (!raw) return EVENT_TYPE_META.map((item) => item.id);
  const allowed = new Set(EVENT_TYPE_META.map((item) => item.id));
  const parsed = raw
    .split(",")
    .map((item) => item.trim())
    .filter((item) => allowed.has(item));
  return parsed.length ? parsed : EVENT_TYPE_META.map((item) => item.id);
}

export default function CalendarPage() {
  const navigate = useNavigate();
  const projects = useProjects();
  const [searchParams, setSearchParams] = useSearchParams();

  const initialView = parseCalendarView(searchParams.get("view"));
  const initialDate = searchParams.get("date") || shortDate(new Date());
  const initialProject = (() => {
    const raw = searchParams.get("project");
    if (!raw) return undefined;
    const parsed = Number(raw);
    return Number.isNaN(parsed) ? undefined : parsed;
  })();
  const initialMetaIds = parseMetaIds(searchParams.get("types"));
  const [selectedMetaIds, setSelectedMetaIds] = useState<string[]>(initialMetaIds);
  const [selectedProjectId, setSelectedProjectId] = useState<number | undefined>(initialProject);
  const [selectedEventId, setSelectedEventId] = useState<string | null>(searchParams.get("event"));
  const [activeDate, setActiveDate] = useState<string>(initialDate);
  const [hoverCard, setHoverCard] = useState<{ x: number; y: number; title: string; summary: string } | null>(null);
  const [visibleRange, setVisibleRange] = useState<{ start: string; end: string; view: CalendarView }>({
    start: new Date().toISOString(),
    end: new Date(Date.now() + 30 * 24 * 60 * 60 * 1000).toISOString(),
    view: initialView,
  });

  const selectedMeta = useMemo(
    () => EVENT_TYPE_META.filter((item) => selectedMetaIds.includes(item.id)),
    [selectedMetaIds],
  );
  const selectedEventTypes = useMemo(
    () => Array.from(new Set(selectedMeta.map((item) => item.value))) as CalendarEventType[],
    [selectedMeta],
  );
  const selectedPipelineStatuses = useMemo(
    () =>
      new Set(
        selectedMeta
          .map((item) => item.pipelineStatus)
          .filter((status): status is string => Boolean(status)),
      ),
    [selectedMeta],
  );

  const toggleMetaId = (id: string) => {
    setSelectedMetaIds((current) => {
      if (current.includes(id)) {
        if (current.length === 1) return current;
        return current.filter((value) => value !== id);
      }
      return [...current, id];
    });
  };

  const eventTypesForQuery = selectedEventTypes.length ? selectedEventTypes : ALL_EVENT_TYPES;
  const events = useCalendarEvents({
    start: visibleRange.start,
    end: visibleRange.end,
    view: visibleRange.view,
    eventTypes: eventTypesForQuery,
    projectIds: selectedProjectId ? [selectedProjectId] : [],
    grouping: "auto",
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    limit: 1200,
  });

  const detailQuery = useCalendarEventDetail(
    selectedEventId ?? undefined,
    selectedProjectId,
    Intl.DateTimeFormat().resolvedOptions().timeZone,
  );

  const filteredEvents = useMemo(() => {
    if (!selectedPipelineStatuses.size) {
      return events.data ?? [];
    }
    return (events.data ?? []).filter((event) => {
      if (event.eventType !== "pipeline_started") return true;
      const status = String((event.summary as { status?: string }).status ?? "").toUpperCase();
      const isWarning = status.includes("WARNING");
      if (isWarning) {
        return selectedPipelineStatuses.has("FINISHED_WITH_WARNINGS");
      }
      if (status.includes("FINISH")) {
        return selectedPipelineStatuses.has("FINISHED");
      }
      return true;
    });
  }, [events.data, selectedPipelineStatuses]);
  const uiEvents = useMemo(() => filteredEvents.map(mapCalendarEventToUi), [filteredEvents]);
  const orderedEventIds = useMemo(() => filteredEvents.map((item) => item.id), [filteredEvents]);
  const eventById = useMemo(
    () => new Map(filteredEvents.map((item) => [item.id, item])),
    [filteredEvents],
  );
  const selectedEventFromGrid = selectedEventId ? eventById.get(selectedEventId) ?? null : null;
  const selectedEvent = detailQuery.data ?? selectedEventFromGrid;

  const projectOptions = useMemo(
    () => [
      { value: "all", label: "All projects" },
      ...((projects.data ?? []).map((project) => ({ value: String(project.id), label: project.name }))),
    ],
    [projects.data],
  );

  useEffect(() => {
    if (!selectedEventId) return;
    if (eventById.has(selectedEventId)) return;
    if (detailQuery.data?.id === selectedEventId) return;
    setSelectedEventId(null);
  }, [eventById, selectedEventId, detailQuery.data]);

  useEffect(() => {
    const next = new URLSearchParams(searchParams);
    next.set("view", visibleRange.view);
    next.set("date", activeDate);
    if (selectedProjectId) next.set("project", String(selectedProjectId));
    else next.delete("project");
    next.set("types", selectedMetaIds.join(","));
    if (selectedEventId) next.set("event", selectedEventId);
    else next.delete("event");
    if (next.toString() !== searchParams.toString()) {
      setSearchParams(next, { replace: true });
    }
  }, [
    searchParams,
    setSearchParams,
    visibleRange.view,
    activeDate,
    selectedProjectId,
    selectedMetaIds,
    selectedEventId,
  ]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) {
        return;
      }
      if (event.key === "Escape") {
        setSelectedEventId(null);
        return;
      }
      if (!orderedEventIds.length) return;
      if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      const currentIndex = selectedEventId ? orderedEventIds.indexOf(selectedEventId) : -1;
      const delta = event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 1;
      const nextIndex = currentIndex < 0
        ? (delta > 0 ? 0 : orderedEventIds.length - 1)
        : Math.max(0, Math.min(orderedEventIds.length - 1, currentIndex + delta));
      setSelectedEventId(orderedEventIds[nextIndex]);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [orderedEventIds, selectedEventId]);

  const eventKeyHandlersRef = useRef(new Map<HTMLElement, (ev: KeyboardEvent) => void>());

  if (events.isError) {
    return (
      <PageErrorState
        error={events.error}
        fallbackTitle="Calendar unavailable"
      />
    );
  }

  return (
    <section className="space-y-4">
      <header className="flex flex-col gap-3 rounded-2xl border border-night-500 bg-night-700/90 p-4">
        <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <h1 className="text-lg font-semibold text-slate-100">Calendar</h1>
            <p className="text-sm text-slate-400">Timeline of pipeline, project, and finding events.</p>
          </div>
          <div className="flex w-full flex-wrap items-center gap-4 lg:w-auto lg:justify-end">
            <div className="flex items-center gap-2">
              <span className="text-xs text-slate-400 whitespace-nowrap">Project</span>
              <div className="w-[220px]">
                <SelectField
                  label="Project"
                  hideLabel
                  showIndicator={false}
                  value={selectedProjectId ? String(selectedProjectId) : "all"}
                  onChange={(value) => setSelectedProjectId(value && value !== "all" ? Number(value) : undefined)}
                  options={projectOptions}
                  placeholder="All projects"
                />
              </div>
            </div>
            <div className="flex min-w-[360px] flex-1 items-center gap-1">
              <span className="text-xs text-slate-400 whitespace-nowrap">Event type</span>
              <EventTypeSelector
                className="gap-1"
                selected={selectedMetaIds}
                onToggle={toggleMetaId}
              />
            </div>
          </div>
        </div>
      </header>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="relative rounded-2xl border border-night-500 bg-night-700/80 p-2 lg:p-4">
          <FullCalendar
            plugins={[dayGridPlugin, timeGridPlugin, interactionPlugin]}
            initialView={toFullCalendarView(initialView)}
            initialDate={initialDate}
            headerToolbar={{
              left: "prev,next today",
              center: "title",
              right: "dayGridMonth,timeGridWeek,timeGridDay",
            }}
            events={uiEvents}
            dayMaxEvents
            height="auto"
            eventTimeFormat={{ hour: "2-digit", minute: "2-digit", hour12: false }}
            slotLabelFormat={{ hour: "2-digit", minute: "2-digit", hour12: false }}
            eventContent={(arg) => {
              const extended = arg.event.extendedProps;
              const eventType = String(extended.eventType ?? "") as CalendarEventType;
              const status = String((extended.summary as { status?: string }).status ?? "");
              return (
                <div className="fc-aist-event-content">
                  <span className="fc-aist-event-icon">
                    <CalendarEventTypeIcon eventType={eventType} status={status} />
                  </span>
                  <span className="fc-aist-event-text">{arg.event.title}</span>
                </div>
              );
            }}
            eventClick={(info) => {
              setSelectedEventId(info.event.id);
              setHoverCard(null);
            }}
            datesSet={(info) => {
              setVisibleRange({
                start: info.start.toISOString(),
                end: info.end.toISOString(),
                view: calendarViewToApiView(info.view.type),
              });
              setActiveDate(shortDate(info.view.currentStart));
            }}
            eventDidMount={(info) => {
              const summary = info.event.extendedProps.summary as Record<string, unknown>;
              const fullTitle = String(info.event.extendedProps.fullTitle ?? info.event.title);
              info.el.setAttribute("aria-label", `${fullTitle}. ${formatCalendarSummary(summary)}`);
              info.el.setAttribute("tabindex", "0");
              const handler = (ev: KeyboardEvent) => {
                if (ev.key === "Enter" || ev.key === " ") {
                  ev.preventDefault();
                  setSelectedEventId(info.event.id);
                }
                if (ev.key === "Escape") {
                  ev.preventDefault();
                  setSelectedEventId(null);
                }
              };
              eventKeyHandlersRef.current.set(info.el, handler);
              info.el.addEventListener("keydown", handler);
            }}
            eventWillUnmount={(info) => {
              const handler = eventKeyHandlersRef.current.get(info.el);
              if (handler) {
                info.el.removeEventListener("keydown", handler);
                eventKeyHandlersRef.current.delete(info.el);
              }
            }}
            eventMouseEnter={(info) => {
              const summary = info.event.extendedProps.summary as Record<string, unknown>;
              const fullTitle = String(info.event.extendedProps.fullTitle ?? info.event.title);
              const maxX = typeof window !== "undefined" ? window.innerWidth - 340 : info.jsEvent.clientX + 12;
              setHoverCard({
                x: Math.min(info.jsEvent.clientX + 12, maxX),
                y: info.jsEvent.clientY + 12,
                title: fullTitle,
                summary: formatCalendarSummary(summary),
              });
            }}
            eventMouseLeave={() => setHoverCard(null)}
          />
          {events.isLoading ? (
            <div className="px-2 py-3 text-sm text-slate-400">Loading calendar events...</div>
          ) : null}
          {hoverCard ? (
            <div
              className="aist-calendar-hover-card"
              style={{ left: `${hoverCard.x}px`, top: `${hoverCard.y}px` }}
            >
              <div className="text-xs uppercase tracking-[0.16em] text-brand-500">Event</div>
              <div className="mt-1 text-sm font-semibold text-slate-100">{hoverCard.title}</div>
              <div className="mt-2 text-xs text-slate-300">{hoverCard.summary}</div>
            </div>
          ) : null}
        </div>

        <aside className="aist-card p-4 xl:sticky xl:top-6 xl:h-fit">
          <div className="text-xs uppercase tracking-[0.16em] text-slate-400">Event details</div>
          <CalendarEventDetailsPanel
            selectedEvent={selectedEvent}
            selectedProjectId={selectedProjectId}
            isLoading={Boolean(selectedEventId && detailQuery.isLoading && !selectedEvent)}
            onNavigate={(path) => navigate(path)}
          />
        </aside>
      </div>
    </section>
  );
}
