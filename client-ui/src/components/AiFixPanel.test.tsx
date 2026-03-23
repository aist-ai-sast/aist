// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { AiFix } from "../types";
import AiFixPanel from "./AiFixPanel";

afterEach(() => {
  cleanup();
});

function buildFix(overrides?: Partial<AiFix>): AiFix {
  return {
    fixSummary: "Run container as non-root user to reduce attack surface.",
    fixType: "config_change",
    diff: "--- a/Dockerfile\n+++ b/Dockerfile\n+USER appuser",
    diffAvailable: true,
    codeAfter: "USER appuser\nCMD [\"./app\"]",
    stepByStep: [
      "Step 1: Open the Dockerfile",
      "Step 2: Add RUN groupadd -r appgroup",
      "Step 3: Add USER appuser before CMD",
    ],
    testingHint: "Run: docker run --rm <image> whoami — should print appuser.",
    secretsManagement: null,
    suppressionAnnotation: "# hadolint ignore=DL3002",
    ...overrides,
  };
}

describe("AiFixPanel", () => {
  it("shows summary and fixType badge without expanding", () => {
    render(<AiFixPanel fix={buildFix()} />);

    expect(screen.getByText("Run container as non-root user to reduce attack surface.")).toBeInTheDocument();
    expect(screen.getByText("Config Change")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /show fix/i })).toBeInTheDocument();
  });

  it("does not show diff content before expanding", () => {
    render(<AiFixPanel fix={buildFix()} />);

    expect(screen.queryByText("+USER appuser")).not.toBeInTheDocument();
  });

  it("expands to show diff tab by default when diff is present", () => {
    render(<AiFixPanel fix={buildFix()} />);

    fireEvent.click(screen.getByRole("button", { name: /show fix/i }));

    expect(screen.getByText("+USER appuser")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Diff" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Code After" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Steps" })).toBeInTheDocument();
  });

  it("switches to Steps tab and strips 'Step N:' prefix", () => {
    render(<AiFixPanel fix={buildFix()} />);

    fireEvent.click(screen.getByRole("button", { name: /show fix/i }));
    fireEvent.click(screen.getByRole("button", { name: "Steps" }));

    expect(screen.getByText("Open the Dockerfile")).toBeInTheDocument();
    expect(screen.getByText("Add USER appuser before CMD")).toBeInTheDocument();
  });

  it("shows only Steps tab when diff and codeAfter are absent", () => {
    render(<AiFixPanel fix={buildFix({ diff: null, codeAfter: null })} />);

    fireEvent.click(screen.getByRole("button", { name: /show fix/i }));

    expect(screen.queryByRole("button", { name: "Diff" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Code After" })).not.toBeInTheDocument();
    expect(screen.getByText("Open the Dockerfile")).toBeInTheDocument();
  });

  it("reveals testing hint when pill is clicked", () => {
    render(<AiFixPanel fix={buildFix()} />);

    fireEvent.click(screen.getByRole("button", { name: /show fix/i }));
    fireEvent.click(screen.getByRole("button", { name: /how to test/i }));

    expect(screen.getByText(/docker run --rm/)).toBeInTheDocument();
  });

  it("reveals suppression annotation when pill is clicked", () => {
    render(<AiFixPanel fix={buildFix()} />);

    fireEvent.click(screen.getByRole("button", { name: /show fix/i }));
    fireEvent.click(screen.getByRole("button", { name: /suppress annotation/i }));

    expect(screen.getByText("# hadolint ignore=DL3002")).toBeInTheDocument();
    expect(screen.getByText(/paste this comment/i)).toBeInTheDocument();
  });

  it("hides suppress pill when suppressionAnnotation is null", () => {
    render(<AiFixPanel fix={buildFix({ suppressionAnnotation: null })} />);

    fireEvent.click(screen.getByRole("button", { name: /show fix/i }));

    expect(screen.queryByRole("button", { name: /suppress annotation/i })).not.toBeInTheDocument();
  });

  it("collapses back when Hide is clicked", () => {
    render(<AiFixPanel fix={buildFix()} />);

    fireEvent.click(screen.getByRole("button", { name: /show fix/i }));
    expect(screen.getByText("+USER appuser")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /hide/i }));
    expect(screen.queryByText("+USER appuser")).not.toBeInTheDocument();
  });
});
