import { expect, type Page } from "@playwright/test";

let cachedAuthCookies: Awaited<ReturnType<Page["context"]["cookies"]>> | null = null;

export async function loginByApi(page: Page) {
  const username = process.env.PLAYWRIGHT_USERNAME ?? "admin";
  const password = process.env.PLAYWRIGHT_PASSWORD ?? "AdminsLoveIntegrationtests!";

  if (cachedAuthCookies && cachedAuthCookies.length > 0) {
    await page.context().addCookies(cachedAuthCookies);
    const meWithCached = await page.request.get("/api/v2/aist/me/");
    if (meWithCached.status() === 200) {
      return;
    }
    cachedAuthCookies = null;
  }

  await page.request.get("/auth/login/");
  const csrfToken = (await page.context().cookies()).find((cookie) => cookie.name === "csrftoken")?.value;
  expect(csrfToken).toBeTruthy();

  for (let attempt = 0; attempt < 3; attempt += 1) {
    const response = await page.request.post("/api/v2/aist/auth/login/", {
      data: { username, password },
      headers: { "X-CSRFToken": csrfToken as string },
    });
    const body = await response.text();
    if (response.status() === 204) {
      break;
    }
    if (response.status() !== 429 || attempt === 2) {
      expect(response.status(), body).toBe(204);
      break;
    }
    const waitSeconds = Number(body.match(/available in (\d+) seconds/i)?.[1] ?? "2");
    await page.waitForTimeout((waitSeconds + 1) * 1000);
  }

  const meResponse = await page.request.get("/api/v2/aist/me/");
  expect(meResponse.status(), await meResponse.text()).toBe(200);
  cachedAuthCookies = await page.context().cookies();
}

export async function openCalendar(page: Page) {
  await page.goto("/calendar");
  await expect(page.getByRole("heading", { name: "Calendar" })).toBeVisible({ timeout: 30_000 });
  const events = page.locator(".fc-event");
  await expect.poll(async () => events.count(), { timeout: 30_000 }).toBeGreaterThan(0);
}

export async function openPipelines(page: Page) {
  await page.goto("/pipelines");
  await expect(page.getByRole("main").getByText("Pipelines", { exact: true })).toBeVisible({ timeout: 30_000 });
  const cards = page.locator("article[role='button']");
  await expect.poll(async () => cards.count(), { timeout: 30_000 }).toBeGreaterThan(0);
}

export async function openFindings(
  page: Page,
  search = "",
  options: { requireCards?: boolean } = {},
) {
  const { requireCards = true } = options;
  await page.goto(`/findings${search}`);
  await expect(page.getByText(/Findings · Total/i).first()).toBeVisible({ timeout: 30_000 });
  if (requireCards) {
    const cards = page.locator("article[role='button']");
    await expect.poll(async () => cards.count(), { timeout: 30_000 }).toBeGreaterThan(0);
  }
}
