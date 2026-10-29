import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FeedView } from "@/components/feed";
import { Toaster } from "@/components/toaster";
import { api } from "@/lib/api";
import { DEBOUNCE_MS, type Feed, type FeedRow, FeedSource, feedSource, POLL_MS, PRESSED_HIDE_S, waited } from "@/lib/feed";
import { live } from "@/lib/live";
import { initialState, type State } from "@/lib/store";
import type { Approval } from "@/lib/types";

const NOW = 1_000_000;

const approval = (over: Partial<Approval> = {}): Approval => ({
  id: "5f3a9c21",
  session: "a1b2c3d4",
  kind: "approval",
  tool_use_id: "toolu_1",
  state: "shown",
  tool: "Bash",
  input: { command: "make test" },
  cwd: "/work/retinue",
  digest: "d",
  view: { head: ["Разрешение · retinue · сессия a1b2c3d4"] },
  created: NOW - 600,
  at: NOW - 600,
  ...over,
});

const row = (over: Partial<FeedRow>): FeedRow => ({
  key: "k",
  action: "read",
  source: "session",
  session: "a1b2c3d4",
  track: "notes",
  sphere: null,
  sphere_title: null,
  sphere_icon: null,
  title: "Поездка Ольги",
  since: NOW - 1200,
  essence: "Нашёл три отеля",
  ref: "a1b2c3d4",
  ...over,
});

const question = approval({
  id: "q1",
  kind: "question",
  tool: "AskUserQuestion",
  input: { questions: [{ question: "Где жить?", options: [{ label: "Центр" }, { label: "Окраина" }] }] },
  view: { answers: {}, picks: {} } as unknown as Approval["view"],
});

const FEED: Feed = {
  schema: 1,
  count: 5,
  bot: "retinue_bot",
  assistant_at: NOW - 3 * 3600,
  groups: [
    { action: "answer", title: "Ответить на вопрос", rows: [row({ key: "q", action: "answer", source: "question", approval: question, essence: "Где жить?" })] },
    {
      action: "decide",
      title: "Решить",
      rows: [row({ key: "a", action: "decide", source: "approval", approval: approval(), sphere: "travel", sphere_title: "Путешествия", sphere_icon: "✈️" })],
    },
    { action: "choose", title: "Выбрать", rows: [row({ key: "c", action: "choose", summary: 7, replies: ["Бери первый", "Ещё варианты"] })] },
    { action: "read", title: "Прочитать", rows: [row({ key: "r", session: "b2c3d4e5", title: "Пост" })] },
    {
      action: "assistant",
      title: "От ассистентки",
      rows: [row({ key: "w", action: "assistant", source: "chat", session: null, track: null, title: "Анна Петрова", essence: "ответить: трансфер" })],
    },
  ],
};

function mount(node: React.ReactNode) {
  const root = createRootRoute();
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>открыта сессия</p> });
  const router = createRouter({ routeTree: root.addChildren([home, one]), history: createMemoryHistory({ initialEntries: ["/"] }) });
  return render(<RouterProvider router={router} />);
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  live.stop();
});

describe("FeedView", () => {
  it("groups by action, with each row's source, sphere, wait, line and action in place", async () => {
    mount(<FeedView feed={FEED} now={NOW} />);
    expect(await screen.findByRole("heading", { name: "Ждут тебя (5)" })).toBeTruthy();
    const groups = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(groups).toEqual(["Ответить на вопрос (1)", "Решить (1)", "Выбрать (1)", "Прочитать (1)", "От ассистентки (1)"]);

    const answer = screen.getByRole("region", { name: "Ответить на вопрос" });
    expect(within(answer).getByRole("button", { name: /Центр/ })).toBeTruthy();
    const decide = screen.getByRole("region", { name: "Решить" });
    expect(within(decide).getByText("✈️ Путешествия")).toBeTruthy();
    expect(within(decide).getByText("20 мин")).toBeTruthy();
    expect(within(decide).getByRole("button", { name: "Разрешить" })).toBeTruthy();
    const choose = screen.getByRole("region", { name: "Выбрать" });
    expect(within(choose).getByText("Нашёл три отеля")).toBeTruthy();
    expect(within(choose).getByRole("button", { name: "Бери первый" })).toBeTruthy();
    const read = screen.getByRole("region", { name: "Прочитать" });
    expect(within(read).getByRole("link", { name: "Пост" }).getAttribute("href")).toBe("/s/b2c3d4e5");
    const hers = screen.getByRole("region", { name: "От ассистентки" });
    expect(within(hers).getByRole("link", { name: "Открыть в Telegram" }).getAttribute("href")).toBe("https://t.me/retinue_bot");
    expect(within(hers).getByText(/Список ассистентки от/)).toBeTruthy();
    expect(within(hers).queryByRole("link", { name: "Анна Петрова" })).toBeNull();
  });

  it("shows a long call by its line until opened: nothing is decided unseen", async () => {
    const long = approval({ input: { command: "echo длинно && ".repeat(40) } });
    const feed = { ...FEED, count: 1, groups: [{ action: "decide" as const, title: "Решить", rows: [row({ key: "a", action: "decide", source: "approval", approval: long, essence: "echo длинно && echo…" })] }] };
    mount(<FeedView feed={feed} now={NOW} />);
    expect(await screen.findByText("echo длинно && echo…")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Разрешить" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Показать и решить" }));
    expect(screen.getByTestId("approval-input").textContent).toBe(long.input.command);
    expect(screen.getByRole("button", { name: "Разрешить" })).toBeTruthy();
  });

  it("says when nothing waits, and waits for the first read", () => {
    const { rerender } = render(<FeedView feed={null} />);
    expect(screen.getByText("Загружаю…")).toBeTruthy();
    rerender(<FeedView feed={{ ...FEED, count: 0, groups: [] }} empty="Пусто." />);
    expect(screen.getByText("Пусто.")).toBeTruthy();
  });
});

describe("Her row in the feed", () => {
  const T = "0123456789abcdef0123456789abcdef";
  const hers = (rows: FeedRow[], over: Partial<Feed> = {}): Feed => ({
    ...FEED,
    count: rows.length,
    groups: [{ action: "assistant", title: "От ассистентки", rows }],
    ...over,
  });

  it("closes from here: the row's token goes with the press, and the toast takes it back", async () => {
    const press = vi.spyOn(api, "waitingPress").mockResolvedValue({ ok: true });
    const hide = vi.spyOn(feedSource, "hide");
    const unhide = vi.spyOn(feedSource, "unhide");
    const feed = hers([row({ key: "w", action: "assistant", source: "letter", session: null, title: "Анна Петрова", essence: "оплатить: хостинг", t: T })]);
    mount(
      <>
        <FeedView feed={feed} now={NOW} />
        <Toaster />
      </>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Сделано" }));
    await waitFor(() => expect(press).toHaveBeenCalledWith(T, "done"));
    expect(hide).toHaveBeenCalledWith(T);
    const toast = await screen.findByRole("status");
    expect(toast.textContent).toBe("СделаноВернуть");
    fireEvent.click(within(toast).getByRole("button", { name: "Вернуть" }));
    await waitFor(() => expect(press).toHaveBeenLastCalledWith(T, "open"));
    await waitFor(() => expect(unhide).toHaveBeenCalledWith(T));
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("has no buttons on the private matter's row, and counts what closed and what ran out", async () => {
    const feed = hers([row({ key: "p", action: "assistant", source: "letter", session: null, title: "Личное", essence: "2 письма о личном ждут ответа" })], {
      closed_today: 5,
      expired_week: 2,
    });
    mount(<FeedView feed={feed} now={NOW} />);
    const section = await screen.findByRole("region", { name: "От ассистентки" });
    expect(within(section).queryByRole("button", { name: "Сделано" })).toBeNull();
    expect(within(section).getByText("Сегодня закрыто: 5 · Истекло без отметки за неделю: 2")).toBeTruthy();
  });

  it("still counts when nothing of hers waits", async () => {
    mount(<FeedView feed={{ ...FEED, count: 0, groups: [], closed_today: 3, expired_week: 0 }} now={NOW} />);
    expect(await screen.findByText("Сегодня закрыто: 3 · Истекло без отметки за неделю: 0")).toBeTruthy();
  });
});

describe("A choice in the feed", () => {
  it("presses a ping's reply by its place, as under the summary, and shows what went in", async () => {
    const reply = vi.spyOn(api, "reply").mockResolvedValue({
      say: "Принято.",
      summary: { session: "a1b2c3d4", seq: 7, head: "", text: "", at: NOW, replies: [], answered: "Ещё варианты" },
    });
    const feed: Feed = { ...FEED, count: 1, groups: [{ action: "choose", title: "Выбрать", rows: [row({ key: "c", action: "choose", summary: 7, replies: ["Бери первый", "Ещё варианты"] })] }] };
    mount(<FeedView feed={feed} now={NOW} />);
    fireEvent.click(await screen.findByRole("button", { name: "Ещё варианты" }));
    await waitFor(() => expect(reply).toHaveBeenCalledWith("a1b2c3d4", 7, 1));
    expect(live.getState().summaries.a1b2c3d4?.["7"]?.answered).toBe("Ещё варианты");
  });
});

describe("FeedSource", () => {
  function fakeLive() {
    let state: State = initialState;
    const listeners = new Set<() => void>();
    return {
      getState: () => state,
      subscribe: (l: () => void) => {
        listeners.add(l);
        return () => listeners.delete(l);
      },
      set(next: Partial<State>) {
        state = { ...state, ...next };
        for (const l of listeners) l();
      },
    };
  }

  it("reads on start, again once after a burst of session, card or summary changes, and every minute", async () => {
    vi.useFakeTimers();
    const load = vi.fn().mockResolvedValue(FEED);
    const source = new FeedSource({ load, setTimer: setTimeout, clearTimer: clearTimeout });
    const fake = fakeLive();
    source.start(fake);
    await vi.waitFor(() => expect(source.feed).toBe(FEED));
    expect(load).toHaveBeenCalledTimes(1);

    fake.set({ sessions: {} });
    fake.set({ approvals: {} });
    fake.set({ summaries: {} });
    fake.set({ deltas: { a: "пишу…" } });
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS + 10);
    expect(load).toHaveBeenCalledTimes(2);
    fake.set({ deltas: { a: "пишу ещё…" } });
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS + 10);
    expect(load).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(load).toHaveBeenCalledTimes(3);

    load.mockRejectedValueOnce(new Error("offline"));
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(source.feed).toBe(FEED);
    source.stop();
    await vi.advanceTimersByTimeAsync(POLL_MS * 3);
    expect(load).toHaveBeenCalledTimes(4);
    expect(source.feed).toBeNull();
  });
});

describe("A pressed row", () => {
  it("stays hidden while the feed has it, at most ten minutes, and is back on «Вернуть»", async () => {
    const T = "0123456789abcdef0123456789abcdef";
    const withRow: Feed = {
      ...FEED,
      count: 2,
      groups: [{ action: "assistant", title: "От ассистентки", rows: [row({ key: "w", action: "assistant", t: T }), row({ key: "x", action: "assistant" })] }],
    };
    let clock = NOW * 1000;
    const load = vi.fn().mockResolvedValue(withRow);
    const source = new FeedSource({ load, setTimer: () => 0 as unknown as ReturnType<typeof setTimeout>, clearTimer: () => {}, now: () => clock });
    await source.refresh();
    source.hide(T);
    expect(source.feed?.count).toBe(1);
    expect(source.feed?.groups[0]?.rows.map((r) => r.key)).toEqual(["x"]);
    await source.refresh();
    expect(source.feed?.count, "the router has not pushed yet: still hidden").toBe(1);
    source.unhide(T);
    expect(source.feed?.count).toBe(2);

    source.hide(T);
    clock += (PRESSED_HIDE_S + 1) * 1000;
    await source.refresh();
    expect(source.feed?.count, "ten minutes on and still there: the press did not go through").toBe(2);

    source.hide(T);
    load.mockResolvedValueOnce({ ...withRow, count: 1, groups: [{ ...withRow.groups[0], rows: [row({ key: "x", action: "assistant" })] }] });
    await source.refresh();
    expect(source.feed?.count).toBe(1);
    await source.refresh();
    expect(source.feed?.count, "the row came back (another «Вернуть»): it shows, the press is forgotten").toBe(2);
  });
});

describe("waited", () => {
  it("says minutes, hours, days", () => {
    expect([waited(NOW - 30, NOW), waited(NOW - 3 * 3600, NOW), waited(NOW - 2 * 86400, NOW)]).toEqual(["1 мин", "3 ч", "2 дн"]);
  });
});
