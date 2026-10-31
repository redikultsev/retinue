import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FeedView } from "@/components/feed";
import { api } from "@/lib/api";
import { DEBOUNCE_MS, type Feed, type FeedRow, FeedSource, POLL_MS, waited } from "@/lib/feed";
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
    { action: "choose", title: "Choose", rows: [row({ key: "c", action: "choose", summary: 7, replies: ["Take the first", "More options"] })] },
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
    expect(await screen.findByRole("heading", { name: "Waiting for you (5)" })).toBeTruthy();
    const groups = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(groups).toEqual(["Answer a question (1)", "Decide (1)", "Choose (1)", "Read (1)", "From the assistant (1)"]);

    const answer = screen.getByRole("region", { name: "Answer a question" });
    expect(within(answer).getByRole("button", { name: /Centre/ })).toBeTruthy();
    const decide = screen.getByRole("region", { name: "Decide" });
    expect(within(decide).getByText("✈️ Travel")).toBeTruthy();
    expect(within(decide).getByText("20 min")).toBeTruthy();
    expect(within(decide).getByRole("button", { name: "Allow" })).toBeTruthy();
    const choose = screen.getByRole("region", { name: "Choose" });
    expect(within(choose).getByText("Found three hotels")).toBeTruthy();
    expect(within(choose).getByRole("button", { name: "Take the first" })).toBeTruthy();
    const read = screen.getByRole("region", { name: "Read" });
    expect(within(read).getByRole("link", { name: "Post" }).getAttribute("href")).toBe("/s/b2c3d4e5");
    const hers = screen.getByRole("region", { name: "From the assistant" });
    expect(within(hers).getByRole("link", { name: "Open in Telegram" }).getAttribute("href")).toBe("https://t.me/retinue_bot");
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

describe("A choice in the feed", () => {
  it("presses a ping's reply by its place, as under the summary, and shows what went in", async () => {
    const reply = vi.spyOn(api, "reply").mockResolvedValue({
      say: "Accepted.",
      summary: { session: "a1b2c3d4", seq: 7, head: "", text: "", at: NOW, replies: [], answered: "More options" },
    });
    const feed: Feed = { ...FEED, count: 1, groups: [{ action: "choose", title: "Choose", rows: [row({ key: "c", action: "choose", summary: 7, replies: ["Take the first", "More options"] })] }] };
    mount(<FeedView feed={feed} now={NOW} />);
    fireEvent.click(await screen.findByRole("button", { name: "More options" }));
    await waitFor(() => expect(reply).toHaveBeenCalledWith("a1b2c3d4", 7, 1));
    expect(live.getState().summaries.a1b2c3d4?.["7"]?.answered).toBe("More options");
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

describe("waited", () => {
  it("says minutes, hours, days", () => {
    expect([waited(NOW - 30, NOW), waited(NOW - 3 * 3600, NOW), waited(NOW - 2 * 86400, NOW)]).toEqual(["1 min", "3 h", "2 d"]);
  });
});
