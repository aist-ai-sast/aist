import { useCallback, useState } from "react";

/**
 * Open/closed state of a page's filter panel, shared by every page that has one.
 *
 * The rules are the dashboard's, which is where they were settled: the panel starts closed
 * so a phone shows results first, a link that already carries filters opens it so the
 * recipient can see what is being applied, and the viewer's own last choice is remembered
 * for the next visit. Pages differ only in `storageKey`.
 *
 * Storage is a per-visit convenience: a blocked store (private mode) degrades to the
 * default rather than failing, which is why both accessors swallow their errors.
 */

export function filterPanelStorageKey(page: string): string {
  return `aist.${page}.filtersOpen`;
}

function readStoredChoice(key: string): boolean | null {
  try {
    const raw = window.localStorage.getItem(key);
    return raw === null ? null : raw === "1";
  } catch {
    return null;
  }
}

function writeStored(key: string, open: boolean) {
  try {
    window.localStorage.setItem(key, open ? "1" : "0");
  } catch {
    // Storage blocked (private mode): the panel state is a per-visit convenience.
  }
}

export type FilterPanelOpenState = {
  open: boolean;
  toggle: () => void;
};

/**
 * True where the layout puts the panel beside the results rather than above them — the
 * Tailwind `lg` breakpoint. A page whose main job is filtering (Findings) opens the panel
 * by default where it costs nothing, and keeps it closed where it would push the results
 * off the screen. Environments without `matchMedia`, such as jsdom, read as narrow.
 */
export function panelFitsBesideResults(): boolean {
  try {
    return window.matchMedia("(min-width: 1024px)").matches;
  } catch {
    return false;
  }
}

export function useFilterPanelOpen({
  page,
  hasFilters,
  defaultOpen,
}: {
  /** Page identifier; becomes part of the storage key. */
  page: string;
  /** True when the current URL/query already carries filters. */
  hasFilters: boolean;
  /**
   * The page's own starting state, evaluated once on mount, for a page that wants the
   * panel open before the viewer has expressed a preference. Defaults to closed.
   */
  defaultOpen?: () => boolean;
}): FilterPanelOpenState {
  const storageKey = filterPanelStorageKey(page);
  // Read once on mount: a filter applied later should not reopen a panel the viewer closed.
  const [open, setOpen] = useState<boolean>(() => {
    if (hasFilters) return true;
    const stored = readStoredChoice(storageKey);
    return stored ?? (defaultOpen?.() ?? false);
  });

  const toggle = useCallback(() => {
    setOpen((current) => {
      writeStored(storageKey, !current);
      return !current;
    });
  }, [storageKey]);

  return { open, toggle };
}
