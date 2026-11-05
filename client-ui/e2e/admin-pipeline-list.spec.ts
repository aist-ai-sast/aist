import { expect, test, type Page, type Request } from "@playwright/test";
import { loginByApi } from "./support";

// Seeded by `bootstrap_demo_access --demo`: one pipeline left EXECUTING, and finished runs of the same project.
const RUNNING_PIPELINE = "demo-ledger-worker-running";
const FINISHED_PIPELINE = "demo-ledger-worker-run-01"; // finished 30 minutes after launch
const LIST_PATH = "/aist-admin/aist/pipelines/";
const LIST_URL = `${LIST_PATH}?q=${RUNNING_PIPELINE}`;

function seconds(clock: string): number {
  const [h, m, s] = clock.trim().split(":").map(Number);
  return h * 3600 + m * 60 + s;
}

function isTableRefresh(request: Request): boolean {
  return new URL(request.url()).pathname === LIST_PATH && request.headers()["hx-request"] === "true";
}

/** Waits until the next htmx refresh of `selector` has been sent and its answer swapped into the page. */
async function nextRefresh(page: Page, selector: string): Promise<Request> {
  const before = await page.locator(selector).elementHandle();
  const request = page.waitForRequest((r) => r.headers()["hx-request"] === "true", { timeout: 10_000 });
  await page.waitForFunction(
    ([old, sel]) => !(old as Element).isConnected && document.querySelector(sel as string) !== null,
    [before, selector] as const,
    { timeout: 10_000 },
  );
  return request;
}

function runningRow(page: Page) {
  return page.locator("#pipeline-table tr", { hasText: RUNNING_PIPELINE });
}

test.beforeEach(async ({ page }) => {
  await loginByApi(page);
});

test("a running pipeline's duration counts up each second", async ({ page }) => {
  await page.goto(LIST_URL);
  const duration = runningRow(page).locator("[data-elapsed-seconds]");
  await expect(duration).toBeVisible();

  const first = seconds(await duration.innerText());
  await expect.poll(async () => seconds(await duration.innerText()), { timeout: 5_000 }).toBeGreaterThanOrEqual(first + 2);
});

test("the table refreshes itself for the same filters and keeps what the user typed", async ({ page }) => {
  await page.goto(LIST_URL);
  const search = page.locator('input[name="q"]');
  await search.focus();

  const refresh = await nextRefresh(page, "#pipeline-table");

  expect(isTableRefresh(refresh)).toBe(true);
  expect(new URL(refresh.url()).searchParams.get("q")).toBe(RUNNING_PIPELINE);
  await expect(runningRow(page)).toBeVisible();
  await expect(search).toBeFocused();
  await expect(search).toHaveValue(RUNNING_PIPELINE);
});

test("the table does not refresh while the tab is hidden", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
  });
  await page.goto(LIST_URL);

  await expect(page.waitForRequest(isTableRefresh, { timeout: 10_000 })).rejects.toThrow();
});

test("a row of a refreshed table opens the pipeline, its delete icon opens the delete confirmation", async ({ page }) => {
  await page.goto(LIST_URL);
  await nextRefresh(page, "#pipeline-table");

  await runningRow(page).getByText("Demo AIST Ledger Worker").click();
  await expect(page).toHaveURL(new RegExp(`${LIST_PATH}${RUNNING_PIPELINE}/$`));

  await page.goto(LIST_URL);
  await runningRow(page).locator(`a[href$="/${RUNNING_PIPELINE}/delete/"]`).click();
  await expect(page).toHaveURL(new RegExp(`${LIST_PATH}${RUNNING_PIPELINE}/delete/$`));
});

test("a failed refresh marks the table stale and stops its counter", async ({ page }) => {
  await page.goto(LIST_URL);
  await page.route(
    (url) => url.pathname === LIST_PATH,
    (route) => (isTableRefresh(route.request()) ? route.fulfill({ status: 500, body: "error" }) : route.continue()),
  );

  await page.waitForResponse((response) => isTableRefresh(response.request()) && response.status() === 500);

  await expect(page.locator("#pipeline-table")).toHaveAttribute("data-stale", "");
  const duration = runningRow(page).locator("[data-elapsed-seconds]");
  const stopped = await duration.innerText();
  await page.waitForTimeout(2_500);
  await expect(duration).toHaveText(stopped);
});

test("a refresh after the session ended reloads the page instead of showing the response in the table", async ({ page }) => {
  await page.goto(LIST_URL);
  await expect(runningRow(page)).toBeVisible();

  await page.context().clearCookies();
  const reloaded = page.waitForEvent("load");
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await reloaded;

  await expect(page.locator("#pipeline-table")).toHaveCount(0);
});

test("a finished pipeline's page shows its run time in clock and in words", async ({ page }) => {
  await page.goto(`${LIST_PATH}${FINISHED_PIPELINE}/`);

  await expect(page.locator("#aist-duration")).toHaveText("00:30:00");
  await expect(page.locator("#aist-duration-human")).toHaveText(/~ 30\s+minutes/);
});

test("a status change refreshes the pipeline page's status panel in place", async ({ page }) => {
  await page.goto(`${LIST_PATH}${RUNNING_PIPELINE}/`);
  await expect(page.locator("#pipeline-status-container")).toHaveAttribute("data-status", "EXECUTING");

  const refreshed = nextRefresh(page, "#pipeline-status-container");
  await page.evaluate(() => {
    const panel = document.getElementById("pipeline-status-container");
    if (panel) (window as unknown as { htmx: { trigger: (el: Element, name: string) => void } }).htmx.trigger(panel, "aist:status-changed");
  });
  const request = await refreshed;

  expect(new URL(request.url()).pathname).toBe(`${LIST_PATH}${RUNNING_PIPELINE}/`);
  await expect(page.locator("#pipeline-status-container")).toContainText("The pipeline worker is running");
  await expect(page.locator("#logbox")).toHaveCount(1);
});
