import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FeedView } from "@/components/feed";
import { Toaster } from "@/components/toaster";
import { ApiError, api } from "@/lib/api";
import { DEBOUNCE_MS, type Feed, type FeedRow, FeedSource, feedSource, POLL_MS, PRESSED_HIDE_S, type WaitingDetail, waited } from "@/lib/feed";
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
  view: { head: ["Approval · retinue · session a1b2c3d4"] },
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
  title: "Anna's trip",
  since: NOW - 1200,
  essence: "Found three hotels",
  ref: "a1b2c3d4",
  ...over,
});

const question = approval({
  id: "q1",
  kind: "question",
  tool: "AskUserQuestion",
  input: { questions: [{ question: "Where to stay?", options: [{ label: "Centre" }, { label: "Outskirts" }] }] },
  view: { answers: {}, picks: {} } as unknown as Approval["view"],
});

const FEED: Feed = {
  schema: 1,
  count: 5,
  bot: "retinue_bot",
  assistant_at: NOW - 3 * 3600,
  groups: [
    { action: "answer", title: "Answer a question", rows: [row({ key: "q", action: "answer", source: "question", approval: question, essence: "Where to stay?" })] },
    {
      action: "decide",
      title: "Decide",
      rows: [row({ key: "a", action: "decide", source: "approval", approval: approval(), sphere: "travel", sphere_title: "Travel", sphere_icon: "✈️" })],
    },
    { action: "reply", title: "Reply", rows: [row({ key: "c", action: "reply" })] },
    { action: "read", title: "Read", rows: [row({ key: "r", session: "b2c3d4e5", title: "Post" })] },
    {
      action: "assistant",
      title: "From the assistant",
      rows: [row({ key: "w", action: "assistant", source: "chat", session: null, track: null, title: "Anna Petrova", essence: "reply: transfer" })],
    },
  ],
};

function mount(node: React.ReactNode) {
  const root = createRootRoute();
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>session opened</p> });
  const record = createRoute({ getParentRoute: () => root, path: "/spaces/$space/r/$", component: () => <p>record opened</p> });
  const router = createRouter({ routeTree: root.addChildren([home, one, record]), history: createMemoryHistory({ initialEntries: ["/"] }) });
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
    expect(await screen.findByRole("heading", { name: "Waiting for you (5)" })).toBeTruthy();
    const groups = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(groups).toEqual(["Answer a question (1)", "Decide (1)", "Reply (1)", "Read (1)", "From the assistant (1)"]);

    const answer = screen.getByRole("region", { name: "Answer a question" });
    expect(within(answer).getByRole("button", { name: /Centre/ })).toBeTruthy();
    const decide = screen.getByRole("region", { name: "Decide" });
    expect(within(decide).getByText("✈️ Travel")).toBeTruthy();
    expect(within(decide).getByText("20 min")).toBeTruthy();
    expect(within(decide).getByRole("button", { name: "Allow" })).toBeTruthy();
    const reply = screen.getByRole("region", { name: "Reply" });
    expect(within(reply).getByText("Found three hotels")).toBeTruthy();
    expect(within(reply).getByRole("link", { name: "Open and answer" }).getAttribute("href")).toBe("/s/a1b2c3d4");
    const read = screen.getByRole("region", { name: "Read" });
    expect(within(read).getByRole("link", { name: "Post" }).getAttribute("href")).toBe("/s/b2c3d4e5");
    const hers = screen.getByRole("region", { name: "From the assistant" });
    expect(within(hers).getByRole("button", { name: /Anna Petrova/ }).getAttribute("aria-haspopup")).toBe("dialog");
    expect(within(hers).getByTitle("Details").getAttribute("tabindex")).toBe("-1");
    expect(within(hers).queryByRole("link", { name: "Open in Telegram" })).toBeNull();
    expect(within(hers).getByText(/The assistant's list is from/)).toBeTruthy();
    expect(within(hers).queryByRole("link", { name: "Anna Petrova" })).toBeNull();
  });

  it("shows a long call by its line until opened: nothing is decided unseen", async () => {
    const long = approval({ input: { command: "echo long && ".repeat(40) } });
    const feed = { ...FEED, count: 1, groups: [{ action: "decide" as const, title: "Decide", rows: [row({ key: "a", action: "decide", source: "approval", approval: long, essence: "echo long && echo…" })] }] };
    mount(<FeedView feed={feed} now={NOW} />);
    expect(await screen.findByText("echo long && echo…")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Allow" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show and decide" }));
    expect(screen.getByTestId("approval-input").textContent).toBe(long.input.command);
    expect(screen.getByRole("button", { name: "Allow" })).toBeTruthy();
  });

  it("says when nothing waits, and waits for the first read", () => {
    const { rerender } = render(<FeedView feed={null} />);
    expect(screen.getByText("Loading…")).toBeTruthy();
    rerender(<FeedView feed={{ ...FEED, count: 0, groups: [] }} empty="Empty." />);
    expect(screen.getByText("Empty.")).toBeTruthy();
  });
});

describe("Her row in the feed", () => {
  const T = "0123456789abcdef0123456789abcdef";
  const hers = (rows: FeedRow[], over: Partial<Feed> = {}): Feed => ({
    ...FEED,
    count: rows.length,
    groups: [{ action: "assistant", title: "From the assistant", rows }],
    ...over,
  });

  it("closes from here: the row's token goes with the press, and the toast takes it back", async () => {
    const press = vi.spyOn(api, "waitingPress").mockResolvedValue({ ok: true });
    const hide = vi.spyOn(feedSource, "hide");
    const unhide = vi.spyOn(feedSource, "unhide");
    const feed = hers([row({ key: "w", action: "assistant", source: "letter", session: null, title: "Анна Петрова", essence: "pay: hosting", t: T })]);
    mount(
      <>
        <FeedView feed={feed} now={NOW} />
        <Toaster />
      </>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Done" }));
    await waitFor(() => expect(press).toHaveBeenCalledWith(T, "done"));
    expect(hide).toHaveBeenCalledWith(T);
    const toast = await screen.findByRole("status");
    expect(toast.textContent).toBe("DoneUndo");
    fireEvent.click(within(toast).getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(press).toHaveBeenLastCalledWith(T, "open"));
    await waitFor(() => expect(unhide).toHaveBeenCalledWith(T));
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("has no buttons on the private matter's row, and counts what closed and what ran out", async () => {
    const feed = hers([row({ key: "p", action: "assistant", source: "letter", session: null, title: "Private", essence: "2 private letters wait for a reply", private: true })], {
      closed_today: 5,
      expired_week: 2,
    });
    mount(<FeedView feed={feed} now={NOW} />);
    const section = await screen.findByRole("region", { name: "From the assistant" });
    expect(within(section).queryByRole("button", { name: "Done" })).toBeNull();
    expect(within(section).queryByTitle("Details")).toBeNull();
    expect(within(section).getByRole("link", { name: "Open in Telegram" }).getAttribute("href")).toBe("https://t.me/retinue_bot");
    expect(within(section).getByText("Closed today: 5 · Ran out unmarked this week: 2")).toBeTruthy();
  });

  it("still counts when nothing of hers waits", async () => {
    mount(<FeedView feed={{ ...FEED, count: 0, groups: [], closed_today: 3, expired_week: 0 }} now={NOW} />);
    expect(await screen.findByText("Closed today: 3 · Ran out unmarked this week: 0")).toBeTruthy();
  });
});

describe("Her row's sheet", () => {
  const T = "0123456789abcdef0123456789abcdef";
  const GMAIL = "https://mail.google.com/mail/?authuser=me@example.com#all/18c2f0a1b2c3d4e5";
  const hers = (rows: FeedRow[], over: Partial<Feed> = {}): Feed => ({
    ...FEED,
    count: rows.length,
    groups: [{ action: "assistant", title: "From the assistant", rows }],
    ...over,
  });
  const letter = row({ key: "w", action: "assistant", source: "letter", session: null, track: null, title: "Acme Hosting", essence: "pay: the invoice ‹number›", ref: "letter:1", t: T, sphere_title: "Home" });
  const detail: WaitingDetail = {
    kind: "letter",
    who: "Acme Hosting",
    account: "me@example.com",
    asks: [
      { need: "pay", text: "The invoice for May.\nBy Friday.", deadline: "Friday", at: NOW - 3600, open: { url: GMAIL, label: "Open in Gmail" } },
      { need: "pay", text: "A reminder.", deadline: "", at: NOW - 7200, open: { url: GMAIL, label: "Open in Gmail" } },
    ],
    more: 1,
    open: { url: GMAIL, label: "Open in Gmail" },
  };

  it("opens at a tap with what the list says, then fills in what only the router has, and goes where the letter is", async () => {
    let answer: (value: { detail: WaitingDetail }) => void = () => undefined;
    const load = vi.spyOn(api, "waitingDetail").mockReturnValue(new Promise((done) => (answer = done)));
    mount(<FeedView feed={hers([letter])} now={NOW} />);
    fireEvent.click(await screen.findByRole("button", { name: /Acme Hosting/ }));
    const sheet = await screen.findByRole("dialog", { name: "Acme Hosting" });
    expect(load).toHaveBeenCalledWith("letter:1", T);
    expect(within(sheet).getByText("pay: the invoice ‹number›")).toBeTruthy();
    expect(within(sheet).getByRole("status").textContent).toBe("Asking the assistant…");
    expect(within(sheet).getByRole("link", { name: /Ask the assistant/ }).getAttribute("href")).toBe("https://t.me/retinue_bot");
    answer({ detail });
    const gmail = await within(sheet).findByRole("link", { name: /Open in Gmail/ });
    expect(gmail.getAttribute("href")).toBe(GMAIL);
    expect(gmail.getAttribute("rel")).toBe("noopener noreferrer");
    expect(within(sheet).getByText("What they ask (3)")).toBeTruthy();
    expect(within(sheet).getByText(/The invoice for May\./).textContent).toBe("The invoice for May.\nBy Friday.");
    expect(within(sheet).getByText("deadline Friday")).toBeTruthy();
    expect(within(sheet).getByText("And 1 older — in Telegram.")).toBeTruthy();
    expect(within(sheet).getByText(/to me@example.com/)).toBeTruthy();
    expect(within(sheet).getByText("Pay as the message says, then mark it done.")).toBeTruthy();
  });

  it("closes the row from the sheet too", async () => {
    vi.spyOn(api, "waitingDetail").mockResolvedValue({ detail });
    const press = vi.spyOn(api, "waitingPress").mockResolvedValue({ ok: true });
    mount(<FeedView feed={hers([letter])} now={NOW} />);
    fireEvent.click(await screen.findByTitle("Details"));
    const sheet = await screen.findByRole("dialog", { name: "Acme Hosting" });
    fireEvent.click(within(sheet).getByRole("button", { name: "Tomorrow" }));
    await waitFor(() => expect(press).toHaveBeenCalledWith(T, "later"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("draws no link of a shape the router does not make", async () => {
    const odd = { ...detail, open: { url: "javascript:alert(1)", label: "Open in Gmail" }, asks: [{ ...detail.asks[0]!, open: { url: "https://evil.example", label: "Open the chat" } }] };
    vi.spyOn(api, "waitingDetail").mockResolvedValue({ detail: odd as WaitingDetail });
    mount(<FeedView feed={hers([letter])} now={NOW} />);
    fireEvent.click(await screen.findByTitle("Details"));
    const sheet = await screen.findByRole("dialog");
    await within(sheet).findByText(/The invoice for May/);
    expect(within(sheet).queryByRole("link", { name: /Gmail|chat/ })).toBeNull();
    expect(within(sheet).getByRole("link", { name: /Ask the assistant/ })).toBeTruthy();
  });

  it("says a silent router plainly and asks again on «Try again»; an outdated row refreshes the list", async () => {
    const load = vi
      .spyOn(api, "waitingDetail")
      .mockRejectedValueOnce(new ApiError(504, "The assistant's side did not answer in time."))
      .mockRejectedValueOnce(new ApiError(410, "The assistant no longer has this row."));
    const refresh = vi.spyOn(feedSource, "refresh").mockResolvedValue();
    mount(<FeedView feed={hers([letter])} now={NOW} />);
    fireEvent.click(await screen.findByTitle("Details"));
    const sheet = await screen.findByRole("dialog");
    expect((await within(sheet).findByRole("alert")).textContent).toContain("did not answer");
    fireEvent.click(within(sheet).getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
    expect((await within(sheet).findByText(/out of date/)).textContent).toContain("refreshed");
    expect(within(sheet).queryByRole("button", { name: "Try again" })).toBeNull();
    expect(refresh).toHaveBeenCalled();
  });

  it("shows a draft's text with «Send in Telegram» first, and never asks for the private matter's", async () => {
    const load = vi.spyOn(api, "waitingDetail").mockResolvedValue({
      detail: { kind: "draft", who: "Anna Petrova", account: "me@example.com", asks: [], more: 0, open: { url: GMAIL, label: "Open in Gmail" },
        draft: { text: "Hello Anna,\n\nYes, Friday.", channel: "mail", subject: "Re: the trip", expires: NOW + 3600, state: "shown" } },
    });
    const draft = row({ key: "d", action: "assistant", source: "draft", session: null, track: null, title: "Anna Petrova", essence: "a reply draft waits for «Send»", ref: "draft:0000000c" });
    const secret = row({ key: "p", action: "assistant", source: "draft", session: null, track: null, title: "Private", essence: "a reply draft waits for «Send»", ref: "draft:0000000d", private: true });
    mount(<FeedView feed={hers([draft, secret])} now={NOW} />);
    fireEvent.click(await screen.findByRole("button", { name: /Anna Petrova/ }));
    const sheet = await screen.findByRole("dialog");
    expect(load).toHaveBeenCalledWith("draft:0000000c", undefined);
    expect((await within(sheet).findByText(/Hello Anna/)).textContent).toBe("Hello Anna,\n\nYes, Friday.");
    const links = within(sheet).getAllByRole("link").map((a) => a.textContent);
    expect(links).toEqual(["Send in Telegram", "Open the thread"]);
    expect(within(sheet).queryByRole("button", { name: "Done" })).toBeNull();
    expect(screen.getAllByTitle("Details")).toHaveLength(1);
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("opens a base's date in its Record, with no sheet", async () => {
    const date = row({ key: "dl", action: "assistant", source: "deadline", session: null, track: null, title: "The Base", essence: "tomorrow: Pay for the internet — due", ref: "deadline:1" });
    const load = vi.spyOn(api, "waitingDetail");
    mount(<FeedView feed={hers([date], { deadlines: [{ id: "deadline:1", when: "1970-01-13", title: "x", sphere: "home", sphere_title: "Home", sphere_icon: null, path: "home/journal/2026-05-01-internet.md" }] })} now={NOW} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open the Record" }));
    expect(await screen.findByText("record opened")).toBeTruthy();
    expect(load).not.toHaveBeenCalled();
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
    fake.set({ deltas: { a: "writing…" } });
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS + 10);
    expect(load).toHaveBeenCalledTimes(2);
    fake.set({ deltas: { a: "writing more…" } });
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
  it("stays hidden while the feed has it, at most ten minutes, and is back on «Undo»", async () => {
    const T = "0123456789abcdef0123456789abcdef";
    const withRow: Feed = {
      ...FEED,
      count: 2,
      groups: [{ action: "assistant", title: "From the assistant", rows: [row({ key: "w", action: "assistant", t: T }), row({ key: "x", action: "assistant" })] }],
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
    expect(source.feed?.count, "the row came back (another «Undo»): it shows, the press is forgotten").toBe(2);
  });
});

describe("waited", () => {
  it("says minutes, hours, days", () => {
    expect([waited(NOW - 30, NOW), waited(NOW - 3 * 3600, NOW), waited(NOW - 2 * 86400, NOW)]).toEqual(["1 min", "3 h", "2 d"]);
  });
});
