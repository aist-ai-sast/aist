import { describe, expect, it } from "vitest";

import {
  countReproductionSteps,
  dastOutcomeNarrative,
  inferMarkdownStructure,
  parseStepsToReproduce,
  type DastOutcomeCode,
} from "./dastNarrative";

// Fixtures below are excerpted from a real generic-aist-report.json DAST run
// (2026-07-03_qa_deep_cloud-backend), not synthetic strings — this is the exact
// shape report authors actually produce.
const DEBUG_STEPS = `1. Confirm DEBUG is scoped to the portal, not global — /cdb/* returns clean JSON errors (baseline contrast)
   Request: \`curl -sk -X POST 'https://qa.cloud.hdw.mx/cdb/oauth2/token' -H 'Content-Type: application/json' -d '{bad json'\`
   Expected: Clean JSON error (application/json), no HTML traceback — confirms the /cdb/* C++ service is unaffected
2. Trigger (a) — unauthenticated Django technical-404 leaking the full URLconf / route map
   Request: \`curl -sk 'https://qa.cloud.hdw.mx/api/nonexistent-xyz/'\`
   Expected: HTTP 404 HTML technical-404 page (~26KB) containing the literal 'DEBUG = True'
3. Prod cross-validation (PROD-SAFE) — the same triggers do NOT render tracebacks on prod (DEBUG=False)
   Request: \`curl -s -k 'https://nxvms.com/api/nonexistent-xyz/' | head -c 80\`
   Expected: Prod returns generic errors, no 'DEBUG = True', no URLconf, no settings dump. Confirms prod runs DEBUG=False`;

const DEBUG_MITIGATION =
  "Set DEBUG=False on every internet-facing Django deployment (cloud_portal and channel_partners) on all non-prod/QA stands, matching the prod task-definitions. Add the missing StaticFileNotFound try/except handler on the /dynamic_assets language-template route so a missing lang file returns a clean 404 instead of an unhandled 500. Fix OrganizationFilter so id is filtered as a UUIDField so a non-UUID query param yields a clean DRF 400 instead of a 500. Set ALLOWED_HOSTS to the explicit host list rather than ['*']. Generic hardening: ensure DEBUG defaults to False and require an explicit opt-in only for local dev; keep a WSGI-level generic 500 handler; restrict drf-yasg swagger/schema exposure on deployed stands.";

const DEBUG_IMPACT =
  "- An unauthenticated attacker retrieves the complete internal URLconf (~90 route/name entries) — a ready-made attack-surface map of the whole API.\n- An unauthenticated attacker triggers a technical-500 that discloses internal AWS topology: RDS host nxcloud-qa-db.cnxpejyeucd7.us-west-1.rds.amazonaws.com, ALLOWED_HOSTS=['*'].\n- Value is limited to internal infrastructure topology disclosure useful for recon — NOT direct compromise.";

describe("parseStepsToReproduce", () => {
  it("splits on step-number boundaries, not on every newline", () => {
    const steps = parseStepsToReproduce(DEBUG_STEPS);
    expect(steps).not.toBeNull();
    expect(steps).toHaveLength(3);
  });

  it("splits each step into action/Request/Expected parts", () => {
    const steps = parseStepsToReproduce(DEBUG_STEPS)!;
    const [first] = steps;
    expect(first.parts.map((p) => p.label)).toEqual(["action", "Request", "Expected"]);
    expect(first.parts[0].text).toContain("Confirm DEBUG is scoped to the portal");
    expect(first.parts[1].text).toContain("curl -sk -X POST");
    expect(first.parts[2].text).toContain("Clean JSON error");
  });

  it("returns null for plain prose with no numbered-step convention", () => {
    expect(parseStepsToReproduce("Authenticate as tenant A, then request tenant B's resource.")).toBeNull();
  });

  it("returns null for empty/missing input", () => {
    expect(parseStepsToReproduce(undefined)).toBeNull();
    expect(parseStepsToReproduce(null)).toBeNull();
    expect(parseStepsToReproduce("")).toBeNull();
  });

  it("still recognizes a numbered list with single-line steps (no Request/Expected)", () => {
    const steps = parseStepsToReproduce("1. Authenticate as tenant A\n2. Request tenant B's resource");
    expect(steps).toHaveLength(2);
    expect(steps![0].parts).toEqual([{ label: "action", text: "Authenticate as tenant A" }]);
  });
});

describe("countReproductionSteps", () => {
  it("counts real steps, not raw lines — the regression this fixes", () => {
    // 3 logical steps, 9 raw non-empty lines (action + Request + Expected each) —
    // the old FindingCard countSteps() would have reported 9.
    expect(countReproductionSteps(DEBUG_STEPS)).toBe(3);
  });

  it("falls back to line count for unstructured text", () => {
    expect(countReproductionSteps("first\nsecond\nthird")).toBe(3);
  });

  it("returns 0 for empty input", () => {
    expect(countReproductionSteps(undefined)).toBe(0);
  });
});

describe("inferMarkdownStructure", () => {
  it("leaves an already-bulleted block untouched", () => {
    const result = inferMarkdownStructure(DEBUG_IMPACT);
    expect(result.split("\n")).toHaveLength(3);
    expect(result).toMatch(/^- An unauthenticated attacker retrieves/);
  });

  it("splits a bulletless multi-sentence paragraph into one bullet per sentence", () => {
    const result = inferMarkdownStructure(DEBUG_MITIGATION);
    const lines = result.split("\n");
    expect(lines[0]).toBe("- Set DEBUG=False on every internet-facing Django deployment (cloud_portal and channel_partners) on all non-prod/QA stands, matching the prod task-definitions.");
    expect(lines).toContain("- Set ALLOWED_HOSTS to the explicit host list rather than ['*'].");
  });

  it("nests a 'Label: item; item; item' sentence into a labeled sub-list", () => {
    const result = inferMarkdownStructure(DEBUG_MITIGATION);
    expect(result).toContain("- Generic hardening:");
    expect(result).toContain("  - ensure DEBUG defaults to False and require an explicit opt-in only for local dev");
    expect(result).toContain("  - keep a WSGI-level generic 500 handler");
    expect(result).toContain("  - restrict drf-yasg swagger/schema exposure on deployed stands");
  });

  it("does not force a short 1-2 sentence paragraph into a list", () => {
    const result = inferMarkdownStructure("Rotate the credential immediately. Notify the security team.");
    expect(result).toBe("Rotate the credential immediately. Notify the security team.");
  });

  it("does not split sentence boundaries inside version strings or domains", () => {
    const result = inferMarkdownStructure(
      "Confirmed on prod build 26.1.0.53524. Same impact as qa. See nxvms.com/api for the affected route.",
    );
    // Would incorrectly fragment on "26.1.0.53524" or "nxvms.com/api" if the
    // sentence boundary regex fired on every period instead of only real ones.
    expect(result.split("\n")).toHaveLength(3);
    expect(result).toContain("- Confirmed on prod build 26.1.0.53524.");
    expect(result).toContain("- See nxvms.com/api for the affected route.");
  });

  it("returns an empty string for empty input", () => {
    expect(inferMarkdownStructure(undefined)).toBe("");
    expect(inferMarkdownStructure("")).toBe("");
  });
});

describe("dastOutcomeNarrative", () => {
  it.each([
    ["SUCCESS_WITH_FINDINGS", "DAST scan completed", "success"],
    ["SUCCESS_CLEAN", "DAST scan completed cleanly", "success"],
    ["POLICY_NO_ELIGIBLE_STAND", "No eligible stand", "warning"],
    ["SOURCE_DRIFT", "Source changed during the scan", "warning"],
    ["PROVIDER_FAILED", "DAST provider failed", "warning"],
    ["PROVIDER_CREDENTIALS_EXPIRED", "DAST provider credentials expired", "warning"],
    ["INVALID_RESULT", "DAST result rejected", "warning"],
    ["CANCELLED", "DAST scan cancelled", "neutral"],
    ["TIMEOUT", "DAST scan timed out", "warning"],
  ] as Array<[DastOutcomeCode, string, string]>)(
    "maps %s to a stable public narrative",
    (code, title, tone) => {
      expect(dastOutcomeNarrative(code)).toMatchObject({ title, tone });
    },
  );

  it("does not invent a narrative without a structured code", () => {
    expect(dastOutcomeNarrative(null)).toBeNull();
  });
});
