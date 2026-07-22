// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import StepsToReproduce from "./StepsToReproduce";

afterEach(() => {
  cleanup();
});

const REAL_STEPS = `1. Confirm the access token is live
   Request: \`curl -sk -o /dev/null -w '%{http_code}\\n' https://qa.cloud.hdw.mx/cdb/account/self -H 'Authorization: Bearer <access_token>'\`
   Expected: HTTP 200 — token grants full account access.
2. Log the user out (server-side closeAllUserSessions)
   Request: \`curl -sk -X DELETE https://qa.cloud.hdw.mx/cdb/oauth2/user/self -H 'Authorization: Bearer <access_token>'\`
   Expected: HTTP 200 {"resultCode":"ok"}. The refresh token is now dead.`;

describe("StepsToReproduce", () => {
  it("renders each step as a separate card with its own number, not one number per line", () => {
    render(<StepsToReproduce raw={REAL_STEPS} />);

    // Two steps, not the 6 raw non-empty lines the old naive split would have produced.
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.queryByText("3")).not.toBeInTheDocument();
  });

  it("shows the request command in a distinct block, stripped of wrapping backticks", () => {
    render(<StepsToReproduce raw={REAL_STEPS} />);

    expect(
      screen.getByText((_, node) => node?.tagName === "CODE" && node.textContent === "curl -sk -X DELETE https://qa.cloud.hdw.mx/cdb/oauth2/user/self -H 'Authorization: Bearer <access_token>'"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Request")).toHaveLength(2);
  });

  it("shows the expected result as its own labeled callout", () => {
    render(<StepsToReproduce raw={REAL_STEPS} />);

    expect(screen.getByText(/token grants full account access/)).toBeInTheDocument();
    expect(screen.getAllByText("Expected")).toHaveLength(2);
  });

  it("falls back to safe prose rendering instead of hiding data it can't parse", () => {
    render(<StepsToReproduce raw="Authenticate as tenant A, then request tenant B's resource." />);

    expect(screen.getByText(/Authenticate as tenant A/)).toBeInTheDocument();
    expect(screen.queryByText("Request")).not.toBeInTheDocument();
  });

  it("shows a placeholder for missing data instead of an empty block", () => {
    render(<StepsToReproduce raw={undefined} />);

    expect(screen.getByText("No steps to reproduce were reported.")).toBeInTheDocument();
  });
});
