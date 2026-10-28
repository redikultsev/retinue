import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Feed } from "@/lib/feed";
import { live } from "@/lib/live";
import { dayWords, elapsed, finished, lastLine, running, SEEN_KEY, tickMs, turnStart, Visits } from "@/lib/today";
import type { Item, Session } from "@/lib/types";
import { TodayView } from "@/pages/today";

const NOW = 1_000_000;

const session = (id: string, over: Partial<Session> = {}): Session => ({
  id, track: "notes", lane: "private", kind: "private", state: "idle", created: 1, at: NOW - 600, title: `Сессия ${id}`,
  waiting: 0, ...over,
});

const item = (n: number, data: Omit<Item, "session" | "turn" | "n">): Item => ({ session: "s1", turn: 0, n, ...data }) as Item;

function mount(node: React.ReactNode) {
  const root = createRootRoute();
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>открыта сессия</p> });
  const router = createRouter({ routeTree: root.addChildren([home, one]), history: createMemoryHistory({ initialEntries: ["/"] }) });
  return render(<RouterProvider router={router} />);
}

afterEach(() => {
  vi.restoreAllMocks();
  live.stop();
});

describe("what «Сегодня» folds", () => {
  it("running: the turns going now, the longest first, never one waiting on the owner", () => {
    const list = [
      session("a", { state: "active", at: NOW - 60 }),
      session("b", { state: "active", at: NOW - 600 }),
      session("c", { state: "active", waiting: 1 }),
      session("d", { state: "waiting" }),
      session("e"),
    ];
    expect(running(list).map((s) => s.id)).toEqual(["b", "a"]);
  });

  it("a turn counts from its start, not from the last card it waited on, and ticks by the second at first", () => {
    const resumed = session("r", { state: "active", at: NOW - 5, turn_started: NOW - 900 });
    const older = session("o", { state: "active", at: NOW - 100 }); // a platform before `turn_started`
    expect(turnStart(resumed)).toBe(NOW - 900);
    expect(turnStart(older)).toBe(NOW - 100);
    expect(running([older, resumed]).map((s) => s.id)).toEqual(["r", "o"]);
    expect(tickMs([resumed], NOW)).toBe(30_000);
    expect(tickMs([resumed, session("f", { state: "active", turn_started: NOW - 20 })], NOW)).toBe(1000);
  });

  it("done: finished since the owner last looked and not read, newest first", () => {
    const since = NOW - 3600;
    const list = [
      session("new", { unread: true, at: NOW - 60 }),
      session("older", { unread: true, at: NOW - 600, state: "closed" }),
      session("before", { unread: true, at: NOW - 7200 }),
      session("read", { unread: false, at: NOW - 60 }),
      session("busy", { unread: true, state: "active", at: NOW - 60 }),
      session("asks", { unread: true, waiting: 1, at: NOW - 60 }),
    ];
    expect(finished(list, since).map((s) => s.id)).toEqual(["new", "older"]);
  });

  it("a running session's last line: the text being written, else the newest item that says something", () => {
    const items = [
      item(0, { kind: "message", data: { role: "assistant", text: "План:\n\n- читаю правила транзита" } }),
      item(1, { kind: "tool", data: { id: "t", tool: "Bash", input: { command: "pytest -q" }, result: null, error: false } }),
      item(2, { kind: "result", data: { stop_reason: "", is_error: false, text: "", deferred: null } }),
    ];
    expect(lastLine(items, undefined)).toBe("Bash: pytest -q");
    expect(lastLine(items.slice(0, 1), undefined)).toBe("читаю правила транзита");
    expect(lastLine(items, "Смотрю **второй** вариант\n")).toBe("Смотрю **второй** вариант");
    expect(lastLine([], undefined)).toBe("");
  });

  it("says how long and which day in the owner's words", () => {
    expect([elapsed(NOW - 40, NOW), elapsed(NOW - 720, NOW), elapsed(NOW - 3900, NOW)]).toEqual(["40 с", "12 мин", "1 ч 05 мин"]);
    const tue = new Date(2026, 9, 28, 15, 0);
    expect(dayWords("2026-10-31", tue)).toEqual({ date: "сб 31.10", rel: "через 3 дня", overdue: false });
    expect(dayWords("2026-10-28", tue).rel).toBe("сегодня");
    expect(dayWords("2026-10-29", tue).rel).toBe("завтра");
    expect(dayWords("2026-10-26", tue)).toMatchObject({ rel: "просрочено 2 дня", overdue: true });
  });

  it("«Готово» counts from when the owner last had the app in front of him, kept on the device", () => {
    const kept = new Map<string, string>();
    const storage = { getItem: (k: string) => kept.get(k) ?? null, setItem: (k: string, v: string) => void kept.set(k, v) };
    let t = NOW;
    const first = new Visits(storage, () => t);
    expect(first.getSince()).toBe(NOW - 86_400); // first run: a day back
    t += 100;
    first.hidden();
    expect(kept.get(SEEN_KEY)).toBe(String(NOW + 100));
    t += 30;
    first.shown();
    expect(first.getSince()).toBe(NOW - 86_400); // half a minute away: the same visit
    first.hidden();
    t += 600;
    const told = vi.fn();
    first.subscribe(told);
    first.shown();
    expect(first.getSince()).toBe(NOW + 130);
    expect(told).toHaveBeenCalled();
    expect(new Visits(storage, () => t).getSince()).toBe(NOW + 130); // the next load starts where he left
  });
});

const FEED: Feed = {
  schema: 1,
  count: 3,
  bot: "retinue_bot",
  assistant_at: NOW - 60,
  groups: [
    {
      action: "read",
      title: "Прочитать",
      rows: [
        { key: "read:new", action: "read", source: "session", session: "new", track: "notes", sphere: null, sphere_title: null,
          sphere_icon: null, title: "Сессия new", since: NOW - 60, essence: "Готово: три отеля", ref: "new" },
        { key: "read:old", action: "read", source: "session", session: "old", track: "notes", sphere: null, sphere_title: null,
          sphere_icon: null, title: "Сессия old", since: NOW - 9000, essence: "Старый итог", ref: "old" },
      ],
    },
    {
      action: "assistant",
      title: "От ассистентки",
      rows: [
        { key: "assistant:chat:1", action: "assistant", source: "chat", session: null, track: null, sphere: null, sphere_title: null,
          sphere_icon: null, title: "Анна Петрова", since: NOW - 600, essence: "ответить: нужен ли трансфер", ref: "chat:1" },
        { key: "assistant:deadline:1", action: "assistant", source: "deadline", session: null, track: null, sphere: "personal",
          sphere_title: "Личное", sphere_icon: "🏠", title: "База", since: NOW - 600, essence: "сегодня: Абонемент в спортзал — действует до",
          ref: "deadline:1" },
      ],
    },
  ],
  deadlines: [
    { id: "deadline:1", when: "1970-01-12", title: "сегодня: Абонемент в спортзал — действует до", sphere: "personal", sphere_title: "Личное",
      sphere_icon: "🏠", path: "personal/profile/gym.md" },
    { id: "deadline:2", when: "1970-01-15", title: "через 3 дня: Счёт — срок", sphere: null, sphere_title: null, sphere_icon: null,
      path: null },
  ],
  chips: [
    { key: "backup", text: "бэкап 3 ч назад", level: "ok" },
    { key: "limit", text: "лимит 91 %", level: "bad" },
  ],
};

describe("TodayView", () => {
  it("shows what waits, what runs, what is done, the week's dates and the chips — each once", async () => {
    vi.spyOn(live, "watch").mockResolvedValue(undefined);
    const sessions = [
      session("run", { state: "active", at: NOW - 5, turn_started: NOW - 720, sphere: "travel", title: "Поездка в Энск" }),
      session("new", { unread: true, at: NOW - 60 }),
      session("old", { unread: true, at: NOW - 9000 }),
    ];
    const spheres = [{ name: "travel", title: "Путешествия", icon: "✈️", track: "notes" }];
    mount(<TodayView feed={FEED} sessions={sessions} spheres={spheres} since={NOW - 3600} now={NOW} />);

    const waiting = await screen.findByRole("region", { name: "Ждут тебя" });
    expect(within(waiting).getByText("Сессия old")).toBeTruthy();
    expect(within(waiting).queryByText("Сессия new")).toBeNull(); // it is under «Готово», not twice
    expect(within(waiting).getByRole("heading", { name: /Ждут тебя \(2\)/ })).toBeTruthy();
    expect(within(waiting).queryByText("сегодня: Абонемент в спортзал — действует до")).toBeNull(); // under «Сроки», not twice
    expect(within(waiting).getByText("Анна Петрова")).toBeTruthy();

    const busy = screen.getByRole("region", { name: "Работают" });
    const row = within(busy).getByTestId("running-row");
    expect(row.textContent).toContain("Поездка в Энск");
    expect(row.textContent).toContain("✈️ Путешествия");
    expect(row.textContent).toContain("12 мин");
    expect(row.getAttribute("href")).toBe("/s/run");
    expect(live.watch).toHaveBeenCalledWith("run"); // its transcript is read once for the last line

    const done = screen.getByRole("region", { name: "Готово" });
    expect(within(done).getAllByTestId("done-row").map((r) => r.textContent)).toEqual([expect.stringContaining("Сессия new")]);

    const dates = screen.getByRole("region", { name: "Сроки" });
    const [pass, bill] = within(dates).getAllByTestId("deadline-row");
    expect(within(pass!).getByRole("button").textContent).toContain("сегодня: Абонемент в спортзал — действует до");
    expect(within(bill!).queryByRole("button")).toBeNull(); // no path: nothing to open

    const chips = screen.getByRole("list", { name: "Состояние" });
    expect(within(chips).getAllByRole("listitem").map((c) => c.textContent)).toEqual(["бэкап 3 ч назад", "лимит 91 %"]);
  });

  it("leaves out a section with nothing in it", async () => {
    mount(<TodayView feed={{ ...FEED, groups: [], count: 0, deadlines: [], chips: [] }} sessions={[]} spheres={[]} since={0} now={NOW} />);
    expect(await screen.findByText("Ничего не ждёт тебя.")).toBeTruthy();
    for (const name of ["Работают", "Готово", "Сроки"]) expect(screen.queryByRole("region", { name })).toBeNull();
    expect(screen.queryByRole("list", { name: "Состояние" })).toBeNull();
  });
});
