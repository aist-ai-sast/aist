// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { ApiToken, CreatedApiToken } from "../lib/apiTokens";

const pushToast = vi.fn();
const createMutateAsync = vi.fn();
const deleteMutateAsync = vi.fn();

let mockTokens: ApiToken[] = [];
let mockCreatePending = false;
let mockDeletePending = false;

vi.mock("../lib/apiTokens", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/apiTokens")>();
  return {
    ...actual,
    useMyTokens: () => ({ data: mockTokens, isLoading: false }),
    useCreateToken: () => ({ mutateAsync: createMutateAsync, isPending: mockCreatePending }),
    useDeleteToken: () => ({ mutateAsync: deleteMutateAsync, isPending: mockDeletePending }),
  };
});

vi.mock("./ToastProvider", () => ({
  useToast: () => ({ push: pushToast }),
}));

import MyTokensSection from "./MyTokensSection";

const RAW_SECRET = "aistpat_abcdefghijklmnopqrstuvwxyz012345";

function createdToken(): CreatedApiToken {
  return {
    id: 1,
    name: "CI pipeline",
    scope: "read_only",
    last4: RAW_SECRET.slice(-4),
    created: "2026-01-01T00:00:00Z",
    last_used_at: null,
    expires_at: null,
    token: RAW_SECRET,
  };
}

afterEach(() => {
  cleanup();
});

beforeEach(() => {
  vi.clearAllMocks();
  mockTokens = [];
  mockCreatePending = false;
  mockDeletePending = false;
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
});

describe("MyTokensSection — create/delete are guarded against double-submit", () => {
  it("disables the Create token button and its inputs while a create is pending", () => {
    mockCreatePending = true;
    render(<MyTokensSection />);
    expect(screen.getByRole("button", { name: /Create token/ })).toBeDisabled();
    expect(screen.getByPlaceholderText("e.g. CI pipeline")).toBeDisabled();
  });

  it("disables the Revoke button for a token while its own delete is pending", () => {
    mockTokens = [
      { id: 1, name: "CI pipeline", scope: "read_only", last4: "1234", created: "2026-01-01T00:00:00Z", last_used_at: null, expires_at: null },
    ];
    mockDeletePending = true;
    render(<MyTokensSection />);
    expect(screen.getByRole("button", { name: "Revoke" })).toBeDisabled();
  });
});

describe("MyTokensSection — token name is rendered as literal text, not markup", () => {
  it("renders a malicious token name as escaped text in the token list", () => {
    const xssName = "<script>alert(1)</script>";
    mockTokens = [
      { id: 1, name: xssName, scope: "read_only", last4: "1234", created: "2026-01-01T00:00:00Z", last_used_at: null, expires_at: null },
    ];
    render(<MyTokensSection />);

    const nameNode = screen.getByText(xssName);
    expect(nameNode.textContent).toBe(xssName);
    expect(document.querySelector("script")).toBeNull();
    expect(document.body.innerHTML).not.toContain("<script>alert(1)</script>");
  });
});

describe("MyTokensSection — created-secret lifecycle", () => {
  it("masks the secret by default, never leaking a substring of it", async () => {
    createMutateAsync.mockResolvedValue(createdToken());
    render(<MyTokensSection />);
    fireEvent.change(screen.getByPlaceholderText("e.g. CI pipeline"), { target: { value: "CI pipeline" } });
    fireEvent.click(screen.getByRole("button", { name: /Create token/ }));

    await screen.findByRole("button", { name: "Show" });
    const codeEl = document.querySelector("code");
    expect(codeEl).not.toBeNull();
    expect(codeEl!.textContent).not.toContain("aistpat_");
    expect(codeEl!.textContent).toMatch(/^•+$/);
  });

  it("reveals the exact raw secret only after clicking Show", async () => {
    createMutateAsync.mockResolvedValue(createdToken());
    render(<MyTokensSection />);
    fireEvent.change(screen.getByPlaceholderText("e.g. CI pipeline"), { target: { value: "CI pipeline" } });
    fireEvent.click(screen.getByRole("button", { name: /Create token/ }));

    const showButton = await screen.findByRole("button", { name: "Show" });
    fireEvent.click(showButton);
    expect(screen.getByText(RAW_SECRET)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Hide" }));
    expect(screen.queryByText(RAW_SECRET)).not.toBeInTheDocument();
  });

  it("copies the raw secret to the clipboard regardless of the masked/revealed toggle", async () => {
    createMutateAsync.mockResolvedValue(createdToken());
    render(<MyTokensSection />);
    fireEvent.change(screen.getByPlaceholderText("e.g. CI pipeline"), { target: { value: "CI pipeline" } });
    fireEvent.click(screen.getByRole("button", { name: /Create token/ }));

    const copyButton = await screen.findByRole("button", { name: "Copy" });
    fireEvent.click(copyButton);
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(RAW_SECRET);
  });

  it("clears the secret from the DOM after Done, and never writes it to localStorage/sessionStorage", async () => {
    const setLocal = vi.spyOn(Storage.prototype, "setItem");
    createMutateAsync.mockResolvedValue(createdToken());
    render(<MyTokensSection />);
    fireEvent.change(screen.getByPlaceholderText("e.g. CI pipeline"), { target: { value: "CI pipeline" } });
    fireEvent.click(screen.getByRole("button", { name: /Create token/ }));

    const doneButton = await screen.findByRole("button", { name: "Done" });
    for (const call of setLocal.mock.calls) {
      expect(String(call[1])).not.toContain(RAW_SECRET);
    }

    fireEvent.click(doneButton);
    expect(screen.queryByRole("button", { name: "Done" })).not.toBeInTheDocument();
    expect(screen.queryByText(RAW_SECRET)).not.toBeInTheDocument();
    setLocal.mockRestore();
  });
});
