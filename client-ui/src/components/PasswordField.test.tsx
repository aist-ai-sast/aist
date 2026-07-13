// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import PasswordField from "./PasswordField";

afterEach(() => {
  cleanup();
});

describe("PasswordField", () => {
  it("hides the value by default", () => {
    render(<PasswordField value="secret" onChange={vi.fn()} />);
    const input = screen.getByDisplayValue("secret") as HTMLInputElement;
    expect(input.type).toBe("password");
  });

  it("reveals the value when the eye icon is clicked, and hides it again on a second click", () => {
    render(<PasswordField value="secret" onChange={vi.fn()} />);
    const input = screen.getByDisplayValue("secret") as HTMLInputElement;
    const toggle = screen.getByRole("button", { name: "Show password" });

    fireEvent.click(toggle);
    expect(input.type).toBe("text");
    expect(screen.getByRole("button", { name: "Hide password" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Hide password" }));
    expect(input.type).toBe("password");
  });

  it("forwards onChange", () => {
    const onChange = vi.fn();
    render(<PasswordField value="" onChange={onChange} />);
    fireEvent.change(screen.getByDisplayValue(""), { target: { value: "new-value" } });
    expect(onChange).toHaveBeenCalled();
  });
});
