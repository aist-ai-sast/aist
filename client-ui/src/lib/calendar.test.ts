import { describe, expect, it } from "vitest";

import type { CalendarEvent } from "../types";
import { calendarViewToApiView, mapCalendarEventToUi } from "./calendar";

describe("calendarViewToApiView", () => {
  it("maps day grid", () => {
    expect(calendarViewToApiView("timeGridDay")).toBe("day");
  });

  it("maps week grid", () => {
    expect(calendarViewToApiView("timeGridWeek")).toBe("week");
  });

  it("maps month grid by default", () => {
    expect(calendarViewToApiView("dayGridMonth")).toBe("month");
  });
});

describe("mapCalendarEventToUi", () => {
  it("maps aggregated future event with proper class names and summary", () => {
    const event: CalendarEvent = {
      id: "finding_created:2026-02-20",
      eventType: "finding_created",
      title: "Findings created: 120",
      start: "2026-02-20T00:00:00Z",
      end: null,
      isAllDay: true,
      isAggregated: true,
      count: 120,
      isFuture: true,
      colorVariant: "future_aggregate",
      summary: { severity: { High: 32 } },
      link: null,
    };
    const uiEvent = mapCalendarEventToUi(event);

    expect(uiEvent.classNames).toEqual([
      "fc-aist-future-event",
      "fc-aist-aggregated-event",
      "fc-aist-type-finding_created",
    ]);
    expect(uiEvent.title).toBe("Findings 120");
    expect(uiEvent.extendedProps.isAggregated).toBe(true);
    expect(uiEvent.extendedProps.link).toBeNull();
    expect(uiEvent.extendedProps.summary).toEqual({ severity: { High: 32 } });
  });

  it("maps single past navigable event", () => {
    const event: CalendarEvent = {
      id: "pipeline_started:pipe-123",
      eventType: "pipeline_started",
      title: "Pipeline started: pipe-123",
      start: "2026-02-18T08:11:00Z",
      end: null,
      isAllDay: false,
      isAggregated: false,
      count: 1,
      isFuture: false,
      colorVariant: "past_single",
      summary: { pipeline_id: "pipe-123" },
      link: "/pipelines?project=13",
    };
    const uiEvent = mapCalendarEventToUi(event);

    expect(uiEvent.classNames).toEqual([
      "fc-aist-past-event",
      "fc-aist-single-event",
      "fc-aist-type-pipeline_started",
    ]);
    expect(uiEvent.title).toContain("Started");
    expect(uiEvent.extendedProps.link).toBe("/pipelines?project=13");
  });
});
