// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("../lib/routes", () => ({
  getRoute: (key: string) => `/${key}`,
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, fetchJson: vi.fn() };
});

import { fetchJson } from "../lib/api";
import SetPasswordPage from "./SetPasswordPage";

const mockedFetchJson = vi.mocked(fetchJson);

function setPath(path: string) {
  window.history.pushState({}, "", path);
}

async function fillAndSubmit(password: string) {
  fireEvent.change(screen.getByLabelText("New password"), { target: { value: password } });
  fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: password } });
  fireEvent.click(screen.getByRole("button", { name: "Set password" }));
}

afterEach(() => {
  cleanup();
  window.history.pushState({}, "", "/");
});

describe("SetPasswordPage — malformed invite/reset link", () => {
  it("submits empty uid and token (not a thrown error) when both URL segments are missing", async () => {
    setPath("/auth/set-password/");
    mockedFetchJson.mockResolvedValueOnce({});
    render(<SetPasswordPage />);

    await fillAndSubmit("StrongPass123!");

    expect(mockedFetchJson).toHaveBeenCalledWith(
      "/set_password_api_url",
      expect.objectContaining({
        body: JSON.stringify({
          uid: "",
          token: "",
          new_password: "StrongPass123!",
          new_password_confirm: "StrongPass123!",
        }),
      }),
    );
  });

  it("submits an empty token when only the token URL segment is missing", async () => {
    setPath("/auth/set-password/abc123/");
    mockedFetchJson.mockResolvedValueOnce({});
    render(<SetPasswordPage />);

    await fillAndSubmit("StrongPass123!");

    expect(mockedFetchJson).toHaveBeenCalledWith(
      "/set_password_api_url",
      expect.objectContaining({
        body: JSON.stringify({
          uid: "abc123",
          token: "",
          new_password: "StrongPass123!",
          new_password_confirm: "StrongPass123!",
        }),
      }),
    );
  });

  it("parses uid and token from a well-formed link", async () => {
    setPath("/auth/set-password/abc123/def456/");
    mockedFetchJson.mockResolvedValueOnce({});
    render(<SetPasswordPage />);

    await fillAndSubmit("StrongPass123!");

    expect(mockedFetchJson).toHaveBeenCalledWith(
      "/set_password_api_url",
      expect.objectContaining({
        body: JSON.stringify({
          uid: "abc123",
          token: "def456",
          new_password: "StrongPass123!",
          new_password_confirm: "StrongPass123!",
        }),
      }),
    );
  });
});
