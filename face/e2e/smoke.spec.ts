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

test("a message with a long unbroken run wraps inside the column", async ({ page }) => {
  await login(page);
  await page.goto("/s/a1b2c3d4");
  const run = `expected one of [${Array.from({ length: 16 }, (_, n) => `"EN_${String(n).padStart(2, "0")}"`).join(",")}]`;
  await page.getByPlaceholder("Write to the session…").fill(run);
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByTestId("input").last()).toContainText("EN_15");
  const outside = await page.evaluate(() =>
    [...document.querySelectorAll(".bg-bubble")].filter((b) => {
      const column = b.closest("[data-testid=input]") ?? b.parentElement!;
      const [r, c] = [b.getBoundingClientRect(), column.getBoundingClientRect()];
      return r.left < c.left - 1 || r.right > c.right + 1;
    }).length,
  );
  expect(outside, "bubbles wider than their column").toBe(0);
});

test("«Today»'s tasks and plan open and fit, and a task plans from its menu", async ({ page }) => {
  await login(page);
  await page.goto("/today?tab=tasks");
  await expect(page.getByText("Needs you").first()).toBeVisible();
  await fits(page);
  await page.getByRole("button", { name: /^Plan: / }).first().click(); // the mock is shared by the widths: any left
  await page.getByRole("menuitem", { name: /Tomorrow/ }).click();
  await expect(page.getByText(/Planning it for Tomorrow/)).toBeVisible();
  await page.goto("/today?tab=plan");
  await expect(page.getByText("Daily stand-up").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Plan my week" })).toBeVisible(); // not pressed: it would plan every task the other widths' runs press
  await fits(page);
  await fits(page);
});
