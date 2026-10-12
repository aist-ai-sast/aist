import { vi } from "vitest";

/**
 * jsdom lacks the layout APIs that Radix Popover and cmdk use to position a dropdown and
 * to keep the highlighted row in view. Any test that opens a searchable SelectField, a
 * TagFilter or another cmdk list installs these first; without them the component throws
 * on open rather than failing the assertion you meant to make.
 */
export function installPopoverTestEnv() {
  Element.prototype.scrollIntoView = vi.fn();
  if (!("ResizeObserver" in globalThis)) {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
  }
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false;
  }
  if (!Element.prototype.releasePointerCapture) {
    Element.prototype.releasePointerCapture = () => {};
  }
  if (!Element.prototype.setPointerCapture) {
    Element.prototype.setPointerCapture = () => {};
  }
}
