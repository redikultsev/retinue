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
  await expect(page.getByText("Reading plan for May").first()).toBeVisible();
  await fits(page);
});

test("a session shows its conversation and, apart, what it made", async ({ page }) => {
  await login(page);
  // «Chat» by name: the widths share the mock, and a session another width's message finished opens on «Results»
  await page.goto("/s/a1b2c3d4?tab=chat");
  await expect(page.locator("[data-turn]").first()).toBeVisible();
  await fits(page);
  await page.getByRole("tab", { name: /^Results/ }).click();
  await expect(page.getByRole("list", { name: "Results" }).getByText("gate-trace.md")).toBeVisible();
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

test("a project three folders deep opens from its Space with its files and sub-project", async ({ page }) => {
  await login(page);
  await page.goto("/spaces/work");
  const sessions = page.getByRole("list", { name: "Sessions" });
  await sessions.getByRole("link", { name: /^Acme/ }).click();
  await page.waitForURL("**/spaces/work/p/acme");
  await sessions.getByRole("link", { name: /^Site/ }).click();
  await page.waitForURL("**/spaces/work/p/acme/site");
  await expect(sessions.getByRole("link", { name: /^Blog/ })).toBeVisible();
  await page.getByRole("tab", { name: /^Files/ }).click();
  const files = page.getByRole("list", { name: "Project files" });
  for (const name of ["mockup.png", "brief.pdf", "media/jingle.wav", "media/demo.mp4"]) await expect(files.getByText(name)).toBeVisible();
  await fits(page);
});

test("a project opens on its passport, and its «Operator» is its own sessions' cards and map", async ({ page }) => {
  await login(page);
  await page.goto("/spaces/travel");
  const passport = page.getByRole("region", { name: "Passport" });
  await expect(passport.getByText(/^Trips without surprises/)).toBeVisible();
  await expect(passport.getByText("In scope", { exact: true })).toBeVisible();
  await expect(page.getByRole("list", { name: "Yours" }).getByRole("button", { name: "Discuss the question" }).first()).toBeVisible();
  await expect(page.getByRole("list", { name: "Sessions" }).getByRole("link", { name: /^Lisbon in May/ })).toBeVisible();
  await fits(page);
  await page.getByRole("tab", { name: /^Operator/ }).click();
  await page.getByRole("button", { name: "Map", exact: true }).click();
  const map = page.getByRole("region", { name: "The map" });
  await expect(map.getByRole("region", { name: "Travel" }).getByTestId("map-tile").first()).toBeVisible();
  await expect(map.getByRole("region", { name: "Work" })).toHaveCount(0);
  await fits(page);
});

test("the operator's map (an old «Today» map link lands on it) keeps the sessions in their places and opens a tile's card", async ({ page }) => {
  await login(page);
  await page.goto("/today?mode=map");
  const map = page.getByRole("region", { name: "The map" });
  await expect(map.getByRole("region", { name: "Travel" })).toBeVisible();
  await expect(map.getByRole("region", { name: "Acme" })).toBeVisible();
  const tile = map.getByRole("button", { name: /A host for the Acme site/ });
  await tile.click();
  await expect(page.getByTestId("operator-card").filter({ hasText: "You asked:" }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Dictate the answer" }).first()).toBeVisible();
  await fits(page);
});

test("«Talk to Retinue» is the assistant's chat: a message, her answer as she writes it, a press on her card", async ({ page }) => {
  await login(page);
  await page.goto("/today");
  await page.getByRole("button", { name: "Talk to Retinue" }).first().click();
  const chat = page.getByRole("complementary", { name: "Talk to Retinue" }).or(page.getByRole("dialog", { name: "Talk to Retinue" }));
  const log = chat.getByRole("log", { name: "The conversation" });
  await expect(log.getByText("What is left before the Ensk trip?")).toBeVisible();
  await expect(log.getByText("In Telegram").first()).toBeVisible();
  await fits(page);
  // the widths share the mock: each sends its own words and presses the card that answers them
  const words = `Book a table for ${test.info().project.name}`;
  await chat.getByRole("textbox", { name: "Message to Retinue" }).fill(words);
  await chat.getByRole("button", { name: "Send" }).click();
  await expect(log.getByText(words, { exact: true })).toBeVisible();
  const answer = log.getByTestId("chat-her").filter({ hasText: `Noted: “${words}”` });
  await expect(answer).toBeVisible();
  const card = log.getByTestId("chat-notice").filter({ hasText: `Add «${words}» to your tasks?` });
  await card.getByRole("button", { name: "Add" }).click();
  await expect(card.getByText("Chosen: Add")).toBeVisible();
  await expect(card.getByRole("button")).toHaveCount(0);
  await fits(page);
});

test("a message with a long unbroken run wraps inside the column", async ({ page }) => {
  await login(page);
  await page.goto("/s/a1b2c3d4?tab=chat");
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

test("«Today»'s tasks and schedule — the week's grid — open and fit, and a task plans from its menu", async ({ page }) => {
  await login(page);
  await page.goto("/today?tab=tasks");
  await expect(page.getByText("From people").first()).toBeVisible();
  await fits(page);
  await page.getByRole("button", { name: /^Plan: / }).first().click(); // the mock is shared by the widths: any left
  await page.getByRole("menuitem", { name: /Tomorrow/ }).click();
  await expect(page.getByText(/Planning it for Tomorrow/)).toBeVisible();
  await page.getByRole("tab", { name: "Schedule" }).click();
  await expect(page).toHaveURL(/tab=schedule/);
  await expect(page.getByText("Daily stand-up").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Plan my week" })).toBeVisible(); // not pressed: it would plan every task the other widths' runs press
  await fits(page);
  // «Not working today» holds the rest of the day's window and says so over the grid — «already» for a width after the first
  await page.getByRole("button", { name: "Now", exact: true }).click();
  await page.getByRole("menuitem", { name: "Not working today" }).click();
  await expect(page.getByText(/^Not working till \d\d:\d\d( already)?\./)).toBeVisible();
  // the day, the week and the grid were tabs once: an old link to any lands on «Schedule»
  for (const old of ["day", "week", "plan"]) {
    await page.goto(`/today?tab=${old}`);
    await expect(page.getByRole("tab", { name: "Schedule" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByText("Daily stand-up").first()).toBeVisible();
  }
});

test("the operator lists the sessions' requests, answers an ask by its option and opens a look's thing in place", async ({ page }) => {
  await login(page);
  await page.goto("/operator");
  const cards = page.getByTestId("operator-card");
  await expect(cards.first()).toBeVisible();
  // a card of every kind of turn ending (mock/fixtures.ts `endings`), what waits before what is only to read
  const kinds = await cards.evaluateAll((all) => all.map((c) => c.getAttribute("data-kind") ?? ""));
  for (const kind of ["ask", "approve", "look", "step", "done"]) expect(kinds, kind).toContain(kind);
  const toRead = kinds.map((k) => k === "done" || k === "read");
  expect(toRead.indexOf(true), "waiting cards first").toBeGreaterThan(toRead.lastIndexOf(false));
  await fits(page);

  const ask = page.getByRole("article", { name: "A host for the Acme site" });
  await expect(ask.getByText("You asked:", { exact: true })).toBeVisible();
  await ask.getByRole("button", { name: /^Wait for the third quote/ }).click();
  await expect(ask.getByText("You: «Wait for the third quote»")).toBeVisible();

  const picture = page.getByRole("article", { name: "The site's new header" }).getByRole("img", { name: "mockup.png" });
  await picture.scrollIntoViewIfNeeded();
  await expect.poll(() => picture.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0)).toBe(true);
  const brief = page.getByRole("article", { name: "The brief for the designer" }).locator("iframe[title='brief.pdf']");
  await expect(brief).toHaveAttribute("src", "/api/base-view/work/acme/site/brief.pdf");
  await brief.scrollIntoViewIfNeeded();
  await expect(brief).toBeVisible();
});
