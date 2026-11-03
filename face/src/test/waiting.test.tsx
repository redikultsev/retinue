// «Waiting for you», counted once (lib/waiting.ts) and a person's ask (lib/asks.ts): which rows «Today» lists and which
// are Tasks', which wait and which are only to read, the one count on every place that says it, the one count of a
// project by the one rule of where a row belongs, and a press on an ask — gone from Tasks at once, back on «Undo».
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { inboxLists } from "@/components/inbox";
import { Toaster } from "@/components/toaster";
import { api } from "@/lib/api";
import { asksOf, forgetPressed, PRESSED_HIDE_S, pressAsk } from "@/lib/asks";
import { type Feed, type FeedRow, FeedSource, feedSource } from "@/lib/feed";
import { live } from "@/lib/live";
import { tileOf } from "@/lib/map";
import type { Plan, Task } from "@/lib/plan";
import { forgetProjects, learnProjects, projectOf, setProjects, useProjectOf } from "@/lib/projects";
import type { Session } from "@/lib/types";
import { requests, todayCount, todayRows, waitingIn, waits } from "@/lib/waiting";
import { makeRouter } from "@/router";
import { TodayView } from "@/pages/today";

// «Schedule»'s grid is FullCalendar, which needs a real browser's layout: here it stands in as a mark.
vi.mock("@/components/plan/grid-tab", () => ({ GridTab: () => <p>the week's grid</p> }));

const NOW = 1_000_000;
const T = "0123456789abcdef0123456789abcdef";

const row = (key: string, over: Partial<FeedRow> = {}): FeedRow => ({
  key, action: "reply", source: "session", session: key, track: "notes", sphere: null, sphere_title: null, sphere_icon: null,
  title: `Session ${key}`, since: NOW - 600, essence: "", ref: key, ...over,
});
const hers = (key: string, over: Partial<FeedRow> = {}) =>
  row(key, { action: "assistant", source: "letter", session: null, track: null, title: "Acme Hosting", essence: "pay: the invoice", ref: key, ...over });
const ask = hers("letter:1", { t: T });
const look = row("look", { action: "read", ending: { kind: "look", request: "Redo the header", checks: ["The logo"] } });
const done = row("done", { action: "read", ending: { kind: "done", request: "Write the README", what: "It says how to run." } });
const read = row("read", { action: "read" });

/** The feed as the platform serves it: its count is every row's. */
function feedOf(rows: FeedRow[]): Feed {
  const groups = ["answer", "decide", "step", "reply", "read", "assistant"]
    .map((action) => ({ action: action as FeedRow["action"], title: action, rows: rows.filter((r) => r.action === action) }))
    .filter((g) => g.rows.length);
  return { schema: 1, count: rows.length, bot: "", assistant_at: null, groups, deadlines: [], chips: [] };
}

const session = (id: string, over: Partial<Session> = {}): Session => ({
  id, track: "notes", lane: "private", kind: "private", state: "idle", created: 1, at: NOW, title: `Session ${id}`, waiting: 0, ...over,
});

function plan(rows: Task[] = []): Plan {
  return {
    now: NOW, tz: "UTC",
    tasks: { rows, rev: null, space: "home", goals: [], missing: false, blocks: [] },
    calendar: { events: [], at: NOW, failed: 0, stale: false },
    reminders: [],
    plan: { at: null, plans: {}, holds: [], said: {}, window: { zone: "UTC", opens: "09:00", closes: "23:00" } },
  };
}

function mount(node: React.ReactNode) {
  const root = createRootRoute();
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const router = createRouter({ routeTree: root.addChildren([home]), history: createMemoryHistory({ initialEntries: ["/"] }) });
  return render(<RouterProvider router={router} />);
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  live.stop();
  live.meta = null;
  feedSource.feed = null;
  forgetPressed();
  forgetProjects();
  localStorage.clear();
});

describe("what waits", () => {
  it("is the owner's move but a finished turn: a thing to look at waits, though the feed lists it under «Read»", () => {
    expect([row("r"), look, done, read, hers("draft:1", { source: "draft" })].map(waits)).toEqual([true, true, false, false, true]);
    // one rule on every screen: «Today»'s mark, the map's tile and its number
    const marks = inboxLists(feedOf([look, done]), [], [], [], () => "").waiting.map((i) => (i.kind === "feed" ? i.mark : null));
    expect(marks).toEqual(["agent", "none"]);
    expect(tileOf(session("look"), [look], { look: { n: 4 } })).toMatchObject({ state: "waits", n: 4 });
    expect(tileOf(session("done"), [done], { done: { n: 5 } })).toMatchObject({ state: "done", n: undefined });
  });

  it("lists on «Today» all but a person's ask, which is Tasks' — and counts so", () => {
    const feed = feedOf([row("r"), look, ask, hers("draft:1", { source: "draft" })]);
    expect(todayRows(feed).map((r) => r.key)).toEqual(["r", "look", "draft:1"]);
    expect(todayCount(feed)).toBe(3);
    expect(todayCount(null)).toBe(0);
  });

  it("gives the operator, the map and the microphone the sessions' requests only, a project's with its sub-projects'", () => {
    const of = projectOf(new Set(["work", "work/acme", "work/acme/site", "travel"]));
    const feed = feedOf([
      row("a", { project: "work/acme" }),
      row("b", { project: "travel" }),
      row("c", { action: "read", project: "work/acme/site/docs" }),
      hers("letter:2", { t: T, sphere: "work" }),
    ]);
    expect(requests(feed).map((r) => r.key)).toEqual(["a", "b", "c"]);
    expect(requests(feed, { path: "work/acme", of }).map((r) => r.key)).toEqual(["a", "c"]);
  });
});

describe("the one count", () => {
  beforeEach(() => {
    vi.spyOn(live, "start").mockResolvedValue(undefined);
    vi.spyOn(live, "watch").mockResolvedValue(undefined);
    vi.spyOn(feedSource, "start").mockImplementation(() => undefined);
    live.meta = { schema: 1, owner_ok: true, face_origin: "", tracks: [{ name: "notes", lane: "private", kind: "private" }], spheres: [] };
  });

  async function open(path: string) {
    const router = makeRouter(createMemoryHistory({ initialEntries: [path] }));
    render(<RouterProvider router={router} />);
    await act(async () => {
      await router.load();
    });
  }

  it("says «Today N» — the feed's count less the people's asks — on the tab, the tab bar, the title and the badge — none in the header", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} }));
    const badge = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, setAppBadge: badge, clearAppBadge: vi.fn().mockResolvedValue(undefined) });
    feedSource.feed = feedOf([row("r"), look, ask]);
    await open("/today");
    expect(await screen.findByLabelText("2 waiting")).toBeTruthy(); // the tab bar
    expect(screen.queryByLabelText(/^Waiting for you: /)).toBeNull(); // the header carries no count
    expect(within(screen.getByRole("tablist", { name: "Today" })).getByRole("tab", { name: /^Today/ }).textContent).toBe("Today2");
    await waitFor(() => expect(document.title).toBe("(2) Retinue"));
    expect(badge).toHaveBeenLastCalledWith(2);
  });

  it("counts the cards the sessions wait on until the feed is in", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} }));
    live.dispatch({ type: "list", snapshot: { schema: 1, cursor: 1, sessions: [session("w", { state: "waiting", waiting: 2 }), session("x", { state: "waiting" }), session("a")] } });
    await open("/today");
    await waitFor(() => expect(document.title).toBe("(3) Retinue"));
  });
});

describe("a project's count", () => {
  it("is its and its sub-projects' requests that wait — a thing to look at too, her rows not — the same on every screen", () => {
    const of = projectOf(new Set(["work", "work/acme", "work/acme/site"]));
    const feed = feedOf([
      row("a", { project: "work/acme" }),
      { ...look, project: "work/acme/site" },
      { ...done, project: "work/acme" },
      hers("letter:2", { t: T, sphere: "work" }),
      hers("draft:1", { source: "draft", sphere: "work" }),
    ]);
    const count = waitingIn(feed, [], of);
    expect([count("work"), count("work/acme"), count("work/acme/site"), count("travel"), count("")]).toEqual([2, 2, 1, 0, 0]);
  });

  it("is its sessions that wait until the feed is in", () => {
    const of = projectOf(new Set(["work", "work/acme"]));
    const count = waitingIn(null, [session("w", { project: "work/acme/notes", state: "waiting" }), session("i", { project: "work/acme" })], of);
    expect([count("work"), count("work/acme")]).toEqual([1, 1]);
  });

  it("places a row by one rule: the nearest project known — the list's, the spheres', and those a project's page read", () => {
    const spheres = [{ name: "travel", title: "Travel", icon: "", track: "notes" }];
    const { result } = renderHook(() => useProjectOf(spheres));
    const where = { project: "work/acme/site/docs", sphere: null };
    expect(result.current(where)).toBe("work"); // nothing known of it yet: its folder's top
    expect(result.current({ project: null, sphere: "travel" })).toBe("travel");
    act(() => setProjects([{ name: "work/acme", title: "Acme", parent: "work", questions: 0, tasks: 0 }]));
    expect(result.current(where)).toBe("work/acme");
    act(() => learnProjects(["work/acme/site", "work/acme/site"]));
    expect(result.current(where)).toBe("work/acme/site");
  });
});

describe("a person's ask", () => {
  const tasksView = (p: Plan) => (
    <>
      <TodayView feed={feedSource.feed} sessions={[]} spheres={[]} since={NOW} now={NOW} tab="tasks" onTab={() => undefined} plan={{ plan: p, error: null, reload: () => undefined }} />
      <Toaster />
    </>
  );
  const tasksTab = () => within(screen.getByRole("tablist", { name: "Today" })).getByRole("tab", { name: /^Tasks/ });

  it("is Tasks' until a task names it; a press takes it off Tasks at once and «Undo» brings it back", async () => {
    const press = vi.spyOn(api, "waitingPress").mockResolvedValue({ ok: true });
    feedSource.feed = feedOf([row("r"), ask, hers("letter:3", { t: "f".repeat(32), essence: "reply: the call" })]);
    mount(tasksView(plan([{ id: "t1", fields: { state: "open", title: "Call back", ask: "letter:3" }, version: 1, created: NOW, updated: NOW }])));
    const done = await screen.findByRole("button", { name: "Done: pay: the invoice" });
    expect(screen.queryByRole("button", { name: "Done: reply: the call" })).toBeNull(); // a task names it
    expect(tasksTab().textContent).toBe("Tasks2"); // the open task, and the ask no task names
    expect(screen.getByRole("tab", { name: /^Today/ }).textContent).toBe("Today1"); // never among what waits

    fireEvent.click(done);
    expect(screen.queryByRole("button", { name: "Done: pay: the invoice" })).toBeNull();
    expect(tasksTab().textContent).toBe("Tasks1");
    await waitFor(() => expect(press).toHaveBeenCalledWith(T, "done"));
    const toast = await screen.findByRole("status");
    fireEvent.click(within(toast).getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(press).toHaveBeenLastCalledWith(T, "open"));
    expect(await screen.findByRole("button", { name: "Done: pay: the invoice" })).toBeTruthy();
    expect(tasksTab().textContent).toBe("Tasks2");
  });

  it("comes back with a toast when the press does not go through", async () => {
    vi.spyOn(api, "waitingPress").mockRejectedValue(new Error("offline"));
    feedSource.feed = feedOf([ask]);
    mount(tasksView(plan()));
    fireEvent.click(await screen.findByRole("button", { name: "Done: pay: the invoice" }));
    expect((await screen.findByRole("status")).textContent).toContain("It did not go through");
    expect(screen.getByRole("button", { name: "Done: pay: the invoice" })).toBeTruthy();
  });

  it("stays off while the feed has it, ten minutes at most, and shows again once it left the feed and came back", async () => {
    const press = vi.fn().mockResolvedValue({ ok: true });
    const load = vi.fn().mockResolvedValue(feedOf([ask]));
    const source = new FeedSource({ load, setTimer: () => 0 as unknown as ReturnType<typeof setTimeout>, clearTimer: () => {} });
    const p = plan();
    await source.refresh();
    let clock = NOW * 1000;
    await pressAsk(T, "done", "Done", { press, source, now: () => clock });
    await source.refresh();
    expect(asksOf(source.feed, p, undefined, clock), "the router has not pushed yet: still off").toEqual([]);
    expect(asksOf(source.feed, p, undefined, clock + (PRESSED_HIDE_S + 1) * 1000), "ten minutes on: the press did not go through").toHaveLength(1);

    load.mockResolvedValueOnce(feedOf([]));
    await source.refresh();
    await source.refresh();
    clock += 1000;
    expect(asksOf(source.feed, p, undefined, clock), "it came back (an «Undo» in Telegram): it shows").toHaveLength(1);
    expect(asksOf(source.feed, null)).toEqual([]); // nothing until the plan says what the tasks name
  });
});
