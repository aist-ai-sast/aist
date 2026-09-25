// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Project } from "../types";
import { describeActiveFilters } from "./findingsActiveFilters";
import { DEFAULT_FINDINGS_FILTERS } from "./findingsFilterUrl";

let mockTags: { data?: { names: string[]; counts: Record<string, number> }; isSuccess: boolean } = {
  data: { names: ["api", "auth", "test"], counts: { api: 2, auth: 3, test: 1 } },
  isSuccess: true,
};

vi.mock("./queries", () => ({
  useFindingTagsByProject: () => mockTags,
}));

import { FILTER_TEXT_DEBOUNCE_MS, useFindingsFilterState } from "./useFindingsFilterState";

const PROJECTS = [{ id: 5, name: "dev/cloud_portal" }] as Project[];

function renderFilterHook(initialUrl: string, extraParams?: Record<string, string>) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={[initialUrl]}>{children}</MemoryRouter>
  );
  return renderHook(
    ({ extra }: { extra?: Record<string, string> }) => ({
      filters: useFindingsFilterState({ projects: PROJECTS, extraParams: extra }),
      location: useLocation(),
      navigate: useNavigate(),
    }),
    { wrapper, initialProps: { extra: extraParams } },
  );
}

function searchOf(result: { current: { location: { search: string } } }) {
  return new URLSearchParams(result.current.location.search);
}

describe("useFindingsFilterState", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockTags = {
      data: { names: ["api", "auth", "test"], counts: { api: 2, auth: 3, test: 1 } },
      isSuccess: true,
    };
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("hydrates the filter from a shared link", () => {
    const { result } = renderFilterHook("/dashboard?severity=High&tags=auth&not_tags=test&project_id=5");

    expect(result.current.filters.state.severities).toEqual(["High"]);
    expect(result.current.filters.state.tags).toEqual({ include: ["auth"], exclude: ["test"], matchMode: "any" });
    expect(result.current.filters.state.projectId).toBe(5);
    expect(result.current.filters.debounced.tags.include).toEqual(["auth"]);
  });

  it("writes a chip-style change to the URL at once, replacing the history entry", () => {
    const { result } = renderFilterHook("/dashboard?severity=High,Critical");

    act(() => result.current.filters.apply({ severities: ["High"] }));

    expect(searchOf(result).get("severity")).toBe("High");
  });

  it("debounces free text before it reaches the URL", () => {
    const { result } = renderFilterHook("/findings");

    act(() => result.current.filters.apply({ title: "sql" }));
    expect(result.current.filters.state.title).toBe("sql");
    expect(result.current.filters.debounced.title).toBe("");
    expect(searchOf(result).get("title")).toBeNull();

    act(() => {
      vi.advanceTimersByTime(FILTER_TEXT_DEBOUNCE_MS);
    });
    expect(result.current.filters.debounced.title).toBe("sql");
    expect(searchOf(result).get("title")).toBe("sql");
  });

  it("takes a URL change from outside without the pending debounce writing old text back", () => {
    const { result } = renderFilterHook("/findings?title=old");

    act(() => result.current.navigate("/findings?title=new&severity=Low"));

    expect(result.current.filters.state.title).toBe("new");
    expect(result.current.filters.debounced.title).toBe("new");
    expect(result.current.filters.state.severities).toEqual(["Low"]);

    act(() => {
      vi.advanceTimersByTime(FILTER_TEXT_DEBOUNCE_MS * 2);
    });
    expect(searchOf(result).get("title")).toBe("new");
    expect(result.current.filters.state.title).toBe("new");
  });

  it("clear all resets every field and empties the URL", () => {
    const { result } = renderFilterHook("/dashboard?severity=High&title=sql&tags=auth&active=true");

    act(() => result.current.filters.clearAll());

    expect(result.current.filters.state).toEqual(DEFAULT_FINDINGS_FILTERS);
    expect(result.current.filters.debounced).toEqual(DEFAULT_FINDINGS_FILTERS);
    expect(result.current.location.search).toBe("");
  });

  it("changing the project in the panel drops the project version", () => {
    const { result } = renderFilterHook("/findings?project_id=5&project_version=main");

    act(() => result.current.filters.panelProps.onProjectChange(7));

    expect(result.current.filters.state.projectId).toBe(7);
    expect(result.current.filters.state.projectVersion).toBe("");
  });

  it("exposes the chips of the active-filters bar and removes exactly one", () => {
    const { result } = renderFilterHook("/dashboard?project_id=5&severity=Critical,High&tags=auth");

    expect(result.current.filters.chips).toEqual(
      describeActiveFilters(result.current.filters.state, { projectName: () => "dev/cloud_portal" }),
    );
    const high = result.current.filters.chips.find((chip) => chip.id === "severity:High");
    expect(high).toBeDefined();

    act(() => result.current.filters.removeChip(high!));

    expect(result.current.filters.state.severities).toEqual(["Critical"]);
    expect(searchOf(result).get("tags")).toBe("auth");
  });

  it("writes page-owned parameters next to the filter", () => {
    const { result, rerender } = renderFilterHook("/findings?severity=High");

    rerender({ extra: { page: "3" } });

    expect(searchOf(result).get("page")).toBe("3");
    expect(searchOf(result).get("severity")).toBe("High");
  });

  it("drops a selected tag that the project no longer has", () => {
    mockTags = { data: { names: ["api"], counts: { api: 1 } }, isSuccess: true };
    const { result } = renderFilterHook("/findings?tags=api,gone");

    expect(result.current.filters.state.tags.include).toEqual(["api"]);
  });

  it("keeps the selection when the tag list failed to load", () => {
    mockTags = { data: undefined, isSuccess: false };
    const { result } = renderFilterHook("/findings?tags=api,gone");

    expect(result.current.filters.state.tags.include).toEqual(["api", "gone"]);
  });
});
