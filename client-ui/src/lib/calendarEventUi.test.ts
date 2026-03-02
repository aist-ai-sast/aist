import { describe, expect, it } from "vitest";

import type { CalendarEvent } from "../types";
import {
  buildCalendarActionLinks,
  formatCalendarSummary,
  formatDurationFromSeconds,
  getSeverityDistribution,
} from "./calendarEventUi";

describe("formatCalendarSummary", () => {
  it("formats severity aggregate summary", () => {
    expect(
      formatCalendarSummary({
        severity: { Critical: 2, High: 3, Medium: 1, Low: 0, Info: 0 },
      }),
    ).toBe("Critical: 2 | High: 3 | Medium: 1 | Low: 0 | Info: 0");
  });
});

describe("formatDurationFromSeconds", () => {
  it("formats human readable duration", () => {
    expect(formatDurationFromSeconds(0)).toBe("0 sec");
    expect(formatDurationFromSeconds(59)).toBe("59 sec");
    expect(formatDurationFromSeconds(1800)).toBe("30 min");
    expect(formatDurationFromSeconds(7200)).toBe("2 h");
    expect(formatDurationFromSeconds(8100)).toBe("2 h 15 min");
  });
});

describe("buildCalendarActionLinks", () => {
  it("builds date and project aware links for finding mitigated", () => {
    const event: CalendarEvent = {
      id: "finding_mitigated:2026-02-24",
      eventType: "finding_mitigated",
      title: "Findings mitigated: 5",
      start: "2026-02-24T00:00:00Z",
      end: null,
      isAllDay: true,
      isAggregated: true,
      count: 5,
      isFuture: false,
      colorVariant: "past_aggregate",
      summary: { project_id: 17, active: false },
      link: null,
    };

    const links = buildCalendarActionLinks(event);

    expect(links).toEqual([
      {
        label: "Open mitigated findings for this date",
        to: "/findings?project=17&status_updated_gte=2026-02-24T00%3A00%3A00Z&status_updated_lte=2026-02-24T23%3A59%3A59.999Z&active=false",
      },
    ]);
  });

  it("uses plural findings route for single finding links", () => {
    const event: CalendarEvent = {
      id: "finding_created:232896",
      eventType: "finding_created",
      title: "Finding created",
      start: "2026-02-24T08:30:00Z",
      end: null,
      isAllDay: false,
      isAggregated: false,
      count: 1,
      isFuture: false,
      colorVariant: "finding",
      summary: { finding_id: 232896 },
      link: null,
    };

    const links = buildCalendarActionLinks(event);
    expect(links.some((item) => item.to === "/findings/232896")).toBe(true);
    expect(links.some((item) => item.to === "/finding/232896")).toBe(false);
  });
});

describe("getSeverityDistribution", () => {
  it("returns rows with non-zero severities and total", () => {
    const result = getSeverityDistribution({
      severity: { Critical: 1, High: 3, Medium: 0, Low: 2, Info: 0 },
    });
    expect(result.total).toBe(6);
    expect(result.rows).toEqual([
      { level: "Critical", count: 1 },
      { level: "High", count: 3 },
      { level: "Low", count: 2 },
    ]);
  });
});
