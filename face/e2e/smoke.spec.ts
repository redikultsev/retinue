// The face's main screens open, say what they should and fit the screen, with no error in the page — at each width of
// playwright.config.ts. The mock's fixtures are invented (mock/fixtures.ts).
import { expect, type Page, test } from "@playwright/test";

const CODE = "RTNE7K2QXP"; // the mock's login code (mock/server.ts)

async function login(page: Page) {
  await page.goto(`/login#${CODE}`);
  await page.waitForURL((url) => !url.pathname.startsWith("/login"));
}

async function fits(page: Page) {
  const [scroll, width] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
  expect(scroll, "no sideways scroll").toBeLessThanOrEqual(width);
}

test.beforeEach(async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  (page as Page & { errors: string[] }).errors = errors;
});

test.afterEach(async ({ page }) => {
  expect((page as Page & { errors: string[] }).errors, "errors in the page").toEqual([]);
});

test("the login code opens «Today»", async ({ page }) => {
  await login(page);
  await page.goto("/today");
  await expect(page.getByText("Acme Hosting").first()).toBeVisible();
  await fits(page);
});

test("a session shows its transcript and its summary", async ({ page }) => {
  await login(page);
  await page.goto("/s/a1b2c3d4");
  await expect(page.locator("[data-turn]").first()).toBeVisible();
  await fits(page);
});

test("the changes of a session show a diff", async ({ page }) => {
  await login(page);
  await page.goto("/s/a1b2c3d4?tab=changes");
  await expect(page.getByText(/Tap a line to comment|comment/i).first()).toBeVisible();
  await fits(page);
});

test("the sessions list and the Spaces open", async ({ page }) => {
  await login(page);
  await page.goto("/sessions");
  await expect(page.getByText("Fix the failing test_gate").first()).toBeVisible();
  for (const path of ["/spaces", "/week"]) {
    await page.goto(path);
    await expect(page.locator("main, [role=main], body").first()).toBeVisible();
    await fits(page);
  }
});
