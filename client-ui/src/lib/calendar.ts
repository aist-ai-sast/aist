import type { EventInput } from "@fullcalendar/core";

import type { CalendarEvent } from "../types";

export type CalendarUiEvent = EventInput & {
  extendedProps: {
    eventType: CalendarEvent["eventType"];
    isFuture: boolean;
    isAggregated: boolean;
    summary: CalendarEvent["summary"];
    link: string | null;
    fullTitle: string;
  };
};

export function calendarViewToApiView(viewType: string): "day" | "week" | "month" {
  if (viewType.includes("Day")) return "day";
  if (viewType.includes("Week")) return "week";
  return "month";
}

export function mapCalendarEventToUi(event: CalendarEvent, timeZone?: string): CalendarUiEvent {
  const summary = event.summary as { project_name?: string; status?: string };
  const localTime = new Date(event.start).toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    ...(timeZone ? { timeZone } : {}),
  });
  const compactTitle = (() => {
    if (event.eventType === "finding_created") return `Findings ${event.count}`;
    if (event.eventType === "finding_processed") return `Processed ${event.count}`;
    if (event.eventType === "pipeline_scheduled") {
      return summary.project_name ? `${localTime} ${summary.project_name}` : `${localTime} Scheduled`;
    }
    if (event.eventType === "pipeline_started") {
      return summary.project_name ? `${localTime} ${summary.project_name}` : `${localTime} Started`;
    }
    return "Project";
  })();

  const pipelineStatusClass = (() => {
    if (event.eventType !== "pipeline_started") return null;
    const status = String(summary.status ?? "").toUpperCase();
    if (status.includes("WARNING")) return "fc-aist-pipeline-status-warning";
    if (status.includes("FINISH")) return "fc-aist-pipeline-status-finished";
    return "fc-aist-pipeline-status-other";
  })();

  return {
    id: event.id,
    title: compactTitle,
    start: event.start,
    end: event.end ?? undefined,
    allDay: event.isAllDay,
    classNames: [
      event.isFuture ? "fc-aist-future-event" : "fc-aist-past-event",
      event.isAggregated ? "fc-aist-aggregated-event" : "fc-aist-single-event",
      `fc-aist-type-${event.eventType}`,
      ...(pipelineStatusClass ? [pipelineStatusClass] : []),
    ],
    extendedProps: {
      eventType: event.eventType,
      isFuture: event.isFuture,
      isAggregated: event.isAggregated,
      summary: event.summary,
      link: event.link ?? null,
      fullTitle: event.title,
    },
  };
}
