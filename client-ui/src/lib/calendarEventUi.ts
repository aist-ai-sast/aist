import type { CalendarEvent, CalendarEventType } from "../types";
import { SEVERITY_ORDER, pipelineStatusBadgeClass } from "./badgeStyles";

export type CalendarActionLink = { label: string; to: string };
export type CalendarSeverityLevel = "Critical" | "High" | "Medium" | "Low" | "Info";
export type CalendarSeverityRow = { level: CalendarSeverityLevel; count: number };

export function formatCalendarSummary(summary: Record<string, unknown>) {
  if ("project_name" in summary && typeof summary.project_name === "string") {
    return `Project: ${summary.project_name}`;
  }
  if ("project_id" in summary && typeof summary.project_id === "number") {
    return `Project ID: ${summary.project_id}`;
  }
  if ("reasons" in summary && typeof summary.reasons === "object" && summary.reasons) {
    const reasons = summary.reasons as Record<string, unknown>;
    return Object.entries(reasons)
      .filter(([, value]) => Number(value) > 0)
      .map(([key, value]) => `${key.replaceAll("_", " ")}: ${Number(value)}`)
      .join(" | ");
  }
  if ("severity" in summary && typeof summary.severity === "object" && summary.severity) {
    const sev = summary.severity as Record<string, unknown>;
    return SEVERITY_ORDER
      .map((level) => `${level}: ${Number(sev[level] ?? 0)}`)
      .join(" | ");
  }
  return "Event details available";
}

export function eventTypeLabel(type: CalendarEventType) {
  if (type === "pipeline_started") return "Pipeline Started";
  if (type === "pipeline_scheduled") return "Pipeline Scheduled";
  if (type === "finding_created") return "Finding Created";
  if (type === "finding_processed") return "Finding Processed";
  return "Project Created";
}

export function formatCalendarDateTime(value: string, allDay: boolean, timeZone?: string) {
  const date = new Date(value);
  return date.toLocaleString("en-GB", {
    year: "numeric",
    month: "short",
    day: "2-digit",
    ...(allDay ? {} : { hour: "2-digit", minute: "2-digit", hour12: false }),
    ...(timeZone ? { timeZone } : {}),
  });
}

export function formatDurationFromSeconds(durationSeconds?: number) {
  if (durationSeconds == null || durationSeconds < 0) return null;
  if (durationSeconds < 60) return `${durationSeconds} sec`;
  const totalMinutes = Math.floor(durationSeconds / 60);
  if (totalMinutes < 60) return `${totalMinutes} min`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (!minutes) return `${hours} h`;
  return `${hours} h ${minutes} min`;
}

export function pipelineStatusBadge(status: string) {
  return pipelineStatusBadgeClass(status);
}

export function buildCalendarActionLinks(event: CalendarEvent, selectedProjectId?: number): CalendarActionLink[] {
  const links: CalendarActionLink[] = [];
  const summary = event.summary as {
    finding_id?: number;
    project_id?: number;
  };
  const day = event.start.slice(0, 10);
  const project = summary.project_id ?? selectedProjectId;
  const findingsByDate = project
    ? `/findings?project=${project}&created_from=${day}&created_to=${day}`
    : `/findings?created_from=${day}&created_to=${day}`;
  const pipelinesByDate = project
    ? `/pipelines?project=${project}&created_from=${day}&created_to=${day}`
    : `/pipelines?created_from=${day}&created_to=${day}`;

  if (event.eventType === "finding_created") {
    links.push({ label: "Open findings for this date", to: findingsByDate });
  }
  if (event.eventType === "finding_processed") {
    const offsetMatch = event.start.match(/(Z|[+-]\d{2}:\d{2})$/);
    const dayBounds = offsetMatch
      ? {
          gte: `${day}T00:00:00${offsetMatch[1]}`,
          lte: `${day}T23:59:59.999${offsetMatch[1]}`,
        }
      : { gte: day, lte: day };
    const params = new URLSearchParams({
      ...(project ? { project: String(project) } : {}),
      processed_gte: dayBounds.gte,
      processed_lte: dayBounds.lte,
    });
    links.push({
      label: "Open processed findings for this date",
      to: `/findings?${params.toString()}`,
    });
  }
  if (event.eventType === "pipeline_started") {
    links.push({ label: "Open pipelines for this date", to: pipelinesByDate });
  }
  if (event.eventType === "pipeline_scheduled") {
    if (project) {
      links.push({ label: "Open project pipelines", to: `/pipelines?project=${project}` });
    } else {
      links.push({ label: "Open pipelines", to: "/pipelines" });
    }
  }
  if (event.eventType === "project_created") {
    links.push({ label: "Open projects", to: "/products" });
    if (project) {
      links.push({ label: "Open project findings", to: `/findings?project=${project}` });
      links.push({ label: "Open project pipelines", to: `/pipelines?project=${project}` });
    }
  }
  if (summary.finding_id) {
    links.push({ label: "Open finding", to: `/findings/${summary.finding_id}` });
  }

  return links.filter((item, idx, arr) => arr.findIndex((other) => other.to === item.to) === idx);
}

export function getSeverityDistribution(summary: Record<string, unknown>): {
  rows: CalendarSeverityRow[];
  total: number;
} {
  const levels: CalendarSeverityLevel[] = SEVERITY_ORDER;
  const severity = (summary.severity as Record<string, unknown> | undefined) ?? {};
  const rows = levels
    .map((level) => ({ level, count: Number(severity[level] ?? 0) }))
    .filter((row) => row.count > 0);
  return {
    rows,
    total: rows.reduce((acc, row) => acc + row.count, 0),
  };
}
