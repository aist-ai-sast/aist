// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { filterPanelStorageKey, useFilterPanelOpen } from "./useFilterPanelOpen";

type Harness = { open: boolean; toggle: () => void };

function mount(page: string, hasFilters: boolean, defaultOpen?: () => boolean) {
  const seen: Harness = { open: false, toggle: () => {} };
  function Probe() {
    const state = useFilterPanelOpen({ page, hasFilters, defaultOpen });
    seen.open = state.open;
    seen.toggle = state.toggle;
    return null;
  }
  const view = render(<Probe />);
  return { seen, view };
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(cleanup);

describe("useFilterPanelOpen", () => {
  it("starts closed so a phone shows results before the filter form", () => {
    const { seen } = mount("pipelines", false);
    expect(seen.open).toBe(false);
  });

  it("opens for a shared link that already carries filters", () => {
    const { seen } = mount("findings", true);
    expect(seen.open).toBe(true);
  });

  it("remembers that the viewer opened it, and that they closed it again", () => {
    const first = mount("dashboard", false);
    act(() => first.seen.toggle());
    expect(first.seen.open).toBe(true);
    first.view.unmount();

    const second = mount("dashboard", false);
    expect(second.seen.open).toBe(true);
    act(() => second.seen.toggle());
    second.view.unmount();

    expect(mount("dashboard", false).seen.open).toBe(false);
  });

  it("honours a page that wants the panel open before the viewer has chosen", () => {
    expect(mount("findings", false, () => true).seen.open).toBe(true);
  });

  it("lets the viewer's own choice override that page default, in both directions", () => {
    const first = mount("findings", false, () => true);
    act(() => first.seen.toggle());
    first.view.unmount();

    // Closed by hand: the page default must not reopen it on the next visit.
    expect(mount("findings", false, () => true).seen.open).toBe(false);
  });

  it("keeps each page's choice separate", () => {
    const pipelines = mount("pipelines", false);
    act(() => pipelines.seen.toggle());
    pipelines.view.unmount();

    expect(mount("findings", false).seen.open).toBe(false);
    expect(window.localStorage.getItem(filterPanelStorageKey("pipelines"))).toBe("1");
  });

  it("stays usable when the browser refuses storage", () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage blocked");
    });
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("storage blocked");
    });

    const { seen } = mount("pipelines", false);
    expect(seen.open).toBe(false);
    act(() => seen.toggle());
    expect(seen.open).toBe(true);

    getItem.mockRestore();
    setItem.mockRestore();
  });
});
