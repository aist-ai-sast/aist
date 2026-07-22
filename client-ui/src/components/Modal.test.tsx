// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import Modal from "./Modal";

afterEach(() => {
  cleanup();
});

describe("Modal", () => {
  it("renders children when open", () => {
    render(
      <Modal open onClose={vi.fn()}>
        <p>Modal body</p>
      </Modal>,
    );
    expect(screen.getByText("Modal body")).toBeTruthy();
  });

  it("renders nothing when closed", () => {
    render(
      <Modal open={false} onClose={vi.fn()}>
        <p>Modal body</p>
      </Modal>,
    );
    expect(screen.queryByText("Modal body")).toBeNull();
  });

  it("calls onClose when the overlay is clicked", () => {
    const onClose = vi.fn();
    render(
      <Modal open onClose={onClose}>
        <p>Modal body</p>
      </Modal>,
    );
    fireEvent.mouseDown(screen.getByRole("dialog").parentElement as Element);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("does not close when clicking inside the panel", () => {
    const onClose = vi.fn();
    render(
      <Modal open onClose={onClose}>
        <p>Modal body</p>
      </Modal>,
    );
    fireEvent.mouseDown(screen.getByText("Modal body"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("calls onClose on Escape", () => {
    const onClose = vi.fn();
    render(
      <Modal open onClose={onClose}>
        <p>Modal body</p>
      </Modal>,
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("traps focus within the panel", () => {
    render(
      <Modal open onClose={vi.fn()}>
        <button>First</button>
        <button>Last</button>
      </Modal>,
    );
    const first = screen.getByText("First");
    const last = screen.getByText("Last");

    last.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(document.activeElement).toBe(first);

    first.focus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
  });

  it("moves focus into the dialog on open", () => {
    render(
      <Modal open onClose={vi.fn()}>
        <button>First</button>
      </Modal>,
    );
    expect(document.activeElement).toBe(screen.getByText("First"));
  });
});
