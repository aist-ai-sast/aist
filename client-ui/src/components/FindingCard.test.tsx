// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Finding } from "../types";
import FindingCard from "./FindingCard";

// FindingSnippetPreview (rendered for non-DAST findings) calls useFileSnippet,
// which uses react-query's useQuery — mock it out so this test doesn't need a
// QueryClientProvider, matching FindingSnippetPreview.test.tsx's own approach.
vi.mock("../lib/snippetCache", () => ({
  useFileSnippet: vi.fn(() => ({
    snippet: null,
    isLoading: false,
    isError: false,
    isSourceUnavailable: false,
    isWarming: false,
    error: null,
  })),
}));

afterEach(() => {
  cleanup();
});

function baseFinding(overrides: Partial<Finding> = {}): Finding {
  return {
    id: 1,
    title: "Test finding",
    severity: "High",
    active: true,
    product: "Demo Product",
    filePath: "src/app.py",
    line: 42,
    tool: "",
    ...overrides,
  };
}

describe("FindingCard — DAST treatment", () => {
  it("shows a DAST badge, endpoints preview, and endpoint/step count for a dynamic finding", () => {
    const finding = baseFinding({
      dynamicFinding: true,
      filePath: "",
      line: null,
      affectedEndpoints: ["https://api.example.com/v1/subscriptions/123", "https://api.example.com/v1/orders/9"],
      stepsToReproduce: "1. Authenticate as tenant A\n2. Request tenant B's resource",
    });

    render(<FindingCard finding={finding} onSelect={vi.fn()} />);

    expect(screen.getByText("DAST")).toBeInTheDocument();
    expect(screen.getByText("https://api.example.com/v1/subscriptions/123")).toBeInTheDocument();
    expect(screen.getByText("https://api.example.com/v1/orders/9")).toBeInTheDocument();
    expect(screen.getByText("2 endpoints · 2 steps")).toBeInTheDocument();
    expect(screen.queryByText(/^File:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Line/)).not.toBeInTheDocument();
  });

  it("counts logical steps, not raw lines, when each step spans multiple lines", () => {
    // Real DAST reports write each step as action + "Request:" + "Expected:" —
    // three lines per step. A naive line-count would have reported 9 steps here.
    const finding = baseFinding({
      dynamicFinding: true,
      filePath: "",
      line: null,
      affectedEndpoints: ["https://api.example.com/v1/subscriptions/123"],
      stepsToReproduce:
        "1. Authenticate as tenant A\n   Request: `curl https://api.example.com/v1/login`\n   Expected: HTTP 200\n2. Request tenant B's resource\n   Request: `curl https://api.example.com/v1/subscriptions/456`\n   Expected: HTTP 200 with tenant B's data\n3. Confirm no authorization check is enforced\n   Request: `curl https://api.example.com/v1/subscriptions/789`\n   Expected: HTTP 200, cross-tenant read confirmed",
    });

    render(<FindingCard finding={finding} onSelect={vi.fn()} />);

    expect(screen.getByText("1 endpoint · 3 steps")).toBeInTheDocument();
  });

  it("shows a placeholder when a dynamic finding has no endpoints", () => {
    const finding = baseFinding({ dynamicFinding: true, filePath: "", line: null, affectedEndpoints: [] });

    render(<FindingCard finding={finding} onSelect={vi.fn()} />);

    expect(screen.getByText("No endpoints reported.")).toBeInTheDocument();
    expect(screen.getByText("0 endpoints · 0 steps")).toBeInTheDocument();
  });

  it("is unchanged for a normal SAST finding", () => {
    const finding = baseFinding();

    render(<FindingCard finding={finding} onSelect={vi.fn()} />);

    expect(screen.queryByText("DAST")).not.toBeInTheDocument();
    expect(screen.getByText(/File: src\/app\.py/)).toBeInTheDocument();
    expect(screen.getByText("Line 42")).toBeInTheDocument();
    expect(screen.getByText("Snippet preview unavailable")).toBeInTheDocument();
  });
});
