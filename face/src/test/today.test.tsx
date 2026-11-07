import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Feed, FeedRow } from "@/lib/feed";
import { live } from "@/lib/live";
import type { Plan, Task } from "@/lib/plan";
import {
  dayWords,
  elapsed,
  finished,
  lastLine,
  projectTitle,
  running,
  SEEN_KEY,
  tickMs,
  todaySearch,
  turnStart,
  Visits,
} from "@/lib/today";
import type { Item, Session } from "@/lib/types";
import { Operator } from "@/components/operator";
import { TodayView } from "@/pages/today";

const NOW = 1_000_000;

const session = (id: string, over: Partial<Session> = {}): Session => ({
  id, track: "notes", lane: "private", kind: "private", state: "idle", created: 1, at: NOW - 600, title: `Session ${id}`,
  waiting: 0, ...over,
});

const item = (n: number, data: Omit<Item, "session" | "turn" | "n">): Item => ({ session: "s1", turn: 0, n, ...data }) as Item;

function mount(node: React.ReactNode) {
  const root = createRootRoute();
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>session opened</p> });
  const router = createRouter({ routeTree: root.addChildren([home, one]), history: createMemoryHistory({ initialEntries: ["/"] }) });
  return render(<RouterProvider router={router} />);
}

afterEach(() => {
  vi.restoreAllMocks();
  live.stop();
  localStorage.clear();
});

describe("what «Today» folds", () => {
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
      item(0, { kind: "message", data: { role: "assistant", text: "Plan:\n\n- reading the transit rules" } }),
      item(1, { kind: "tool", data: { id: "t", tool: "Bash", input: { command: "pytest -q" }, result: null, error: false } }),
      item(2, { kind: "result", data: { stop_reason: "", is_error: false, text: "", deferred: null } }),
    ];
    expect(lastLine(items, undefined)).toBe("Bash: pytest -q");
    expect(lastLine(items.slice(0, 1), undefined)).toBe("reading the transit rules");
    expect(lastLine(items, "Looking at the **second** option\n")).toBe("Looking at the **second** option");
    expect(lastLine([], undefined)).toBe("");
  });

  it("says how long and which day in the owner's words", () => {
    expect([elapsed(NOW - 40, NOW), elapsed(NOW - 720, NOW), elapsed(NOW - 3900, NOW)]).toEqual(["40 s", "12 min", "1 h 05 min"]);
    const tue = new Date(2026, 9, 28, 15, 0);
    expect(dayWords("2026-10-31", tue)).toEqual({ date: "Sat 31.10", rel: "in 3 days", overdue: false });
    expect(dayWords("2026-10-28", tue).rel).toBe("today");
    expect(dayWords("2026-10-29", tue).rel).toBe("tomorrow");
    expect(dayWords("2026-10-26", tue)).toMatchObject({ rel: "2 days overdue", overdue: true });
  });

  it("«Done» counts from when the owner last had the app in front of him, kept on the device", () => {
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
      title: "Read",
      rows: [
        { key: "read:new", action: "read", source: "session", session: "new", track: "notes", sphere: null, sphere_title: null,
          sphere_icon: null, title: "Session new", since: NOW - 60, essence: "Done: three hotels", ref: "new" },
        { key: "read:old", action: "read", source: "session", session: "old", track: "notes", sphere: null, sphere_title: null,
          sphere_icon: null, title: "Session old", since: NOW - 9000, essence: "An old result", ref: "old" },
      ],
    },
    {
      action: "assistant",
      title: "From the assistant",
      rows: [
        { key: "assistant:chat:1", action: "assistant", source: "chat", session: null, track: null, sphere: null, sphere_title: null,
          sphere_icon: null, title: "Anna Petrova", since: NOW - 600, essence: "reply: is a transfer needed", ref: "chat:1" },
        { key: "assistant:deadline:1", action: "assistant", source: "deadline", session: null, track: null, sphere: "personal",
          sphere_title: "Personal", sphere_icon: "🏠", title: "Base", since: NOW - 600, essence: "today: Gym membership — valid until",
          ref: "deadline:1" },
      ],
    },
  ],
  deadlines: [
    { id: "deadline:1", when: "1970-01-12", title: "today: Gym membership — valid until", sphere: "personal", sphere_title: "Personal",
      sphere_icon: "🏠", path: "personal/profile/gym.md" },
    { id: "deadline:2", when: "1970-01-15", title: "in 3 days: Invoice — due", sphere: null, sphere_title: null, sphere_icon: null,
      path: null },
  ],
  chips: [
    { key: "backup", text: "backup 3 h ago", level: "ok" },
    { key: "limit", text: "limit 91 %", level: "bad" },
  ],
};

describe("TodayView", () => {
  it("lists what waits, what runs, what is done and the week's dates — each once — under a header of one line", async () => {
    vi.spyOn(live, "watch").mockResolvedValue(undefined);
    const sessions = [
      session("run", { state: "active", at: NOW - 5, turn_started: NOW - 720, sphere: "travel", title: "Trip to Ensk" }),
      session("new", { unread: true, at: NOW - 60 }),
      session("old", { unread: true, at: NOW - 9000 }),
    ];
    const spheres = [{ name: "travel", title: "Travel", icon: "✈️", track: "notes" }];
    mount(<TodayView feed={FEED} sessions={sessions} spheres={spheres} since={NOW - 3600} now={NOW} />);

    const waiting = await screen.findByRole("region", { name: "Waiting for you" });
    expect(within(waiting).getByText("Session old")).toBeTruthy();
    expect(within(waiting).queryByText("Session new")).toBeNull(); // it is under «Done», not twice
    expect(within(waiting).queryByText(/Gym membership/)).toBeNull(); // under «Dates», not twice
    expect(within(waiting).getByText("Anna Petrova")).toBeTruthy();
    expect(screen.getByText("· 3 waiting")).toBeTruthy(); // the feed's count, as on the sidebar

    const busy = screen.getByRole("region", { name: "Working" });
    const row = within(busy).getByTestId("inbox-row");
    expect(row.textContent).toContain("Trip to Ensk");
    expect(row.textContent).toContain("12 min");
    expect(live.watch).toHaveBeenCalledWith("run"); // its transcript is read once for the last line

    const done = screen.getByRole("region", { name: "Done since you looked" });
    expect(within(done).getAllByTestId("inbox-row").map((r) => r.textContent)).toEqual([expect.stringContaining("Session new")]);

    const dates = screen.getByRole("region", { name: "Dates" });
    expect(within(dates).getAllByTestId("inbox-row").map((r) => r.textContent)).toEqual([
      expect.stringContaining("Gym membership — valid until"),
      expect.stringContaining("Invoice — due"),
    ]);
    expect(within(dates).getAllByTestId("inbox-row")[0]!.textContent).toMatch(/^today/);

    const status = screen.getByRole("list", { name: "Status" });
    expect(within(status).getAllByRole("listitem").map((c) => c.textContent)).toEqual(["backup 3 h ago", "limit 91 %"]);

    fireEvent.click(row);
    const pane = await screen.findByRole("region", { name: "Trip to Ensk" });
    expect(within(pane).getByText(/Travel · working for 12 min/)).toBeTruthy();
    expect(within(pane).getAllByRole("link", { name: /Open the session/ })[0]!.getAttribute("href")).toBe("/s/run");
  });

  it("leaves out a part with nothing in it", async () => {
    mount(<TodayView feed={{ ...FEED, groups: [], count: 0, deadlines: [], chips: [] }} sessions={[]} spheres={[]} since={0} now={NOW} />);
    expect(await screen.findByText("Nothing is waiting for you.")).toBeTruthy();
    for (const name of ["Working", "Done since you looked", "Dates"]) expect(screen.queryByRole("region", { name })).toBeNull();
    expect(screen.queryByRole("list", { name: "Status" })).toBeNull();
  });
});

// --- by project, and the operator ---------------------------------------------------------------------------------

const row = (key: string, over: Partial<FeedRow>): FeedRow => ({
  key,
  action: "read",
  source: "session",
  session: null,
  track: "notes",
  sphere: null,
  sphere_title: null,
  sphere_icon: null,
  title: key,
  since: NOW - 600,
  essence: "",
  ref: key,
  ...over,
});

const task = (id: string, fields: Task["fields"]): Task => ({ id, fields: { state: "open", ...fields }, version: 1, created: NOW - 9000, updated: NOW - 9000 });

function plan(rows: Task[]): Plan {
  return {
    now: NOW,
    tz: "UTC",
    tasks: { rows, rev: null, space: "home", goals: [{ value: "work", label: "Work" }], missing: false, blocks: [] },
    calendar: { events: [], at: NOW, failed: 0, stale: false },
    reminders: [],
    plan: { at: null, plans: {}, holds: [], said: {}, window: { zone: "UTC", opens: "09:00", closes: "23:00" } },
  };
}

const SPHERES = [{ name: "travel", title: "Travel", icon: "", track: "notes" }];

const BY_PROJECT: Feed = {
  schema: 1,
  count: 3,
  bot: "retinue_bot",
  assistant_at: NOW - 60,
  deadlines: [],
  chips: [],
  groups: [
    {
      action: "read",
      title: "Read",
      rows: [
        row("read:old", { session: "old", title: "Session old", project: "work/acme" }),
        row("read:loose", { session: "loose", title: "Session loose", project: null }),
        row("read:trip", { session: "trip", title: "Session trip", project: "travel/ensk" }),
      ],
    },
    {
      action: "assistant",
      title: "From the assistant",
      rows: [
        row("assistant:chat:1", {
          action: "assistant",
          source: "chat",
          track: null,
          title: "Anna Petrova",
          essence: "reply: is a transfer needed",
          ref: "chat:1",
          sphere: "travel",
        }),
      ],
    },
  ],
};

describe("«Today» by action", () => {
  it("names a project in the owner's words", () => {
    expect(projectTitle("travel/ensk", SPHERES)).toBe("Travel / ensk");
    expect(projectTitle("/work/acme/", SPHERES)).toBe("work / acme");
    expect(projectTitle("", SPHERES)).toBe("Other");
  });

  it("test_today_is_by_action_every_row_names_its_project_and_an_old_by_project_lands_on_by_action", async () => {
    // «By project» duplicated the left panel's projects; the operator and the map are a place of their own now
    const tasks = plan([
      task("t1", { title: "Send Acme the draft", project: "work/acme", due: "1970-01-12" }),
      task("t2", { title: "Book the dentist" }),
    ]);
    const view = () => (
      <TodayView feed={BY_PROJECT} sessions={[]} spheres={SPHERES} since={NOW} now={NOW} plan={{ plan: tasks, error: null, reload: () => undefined }} />
    );
    mount(view());

    expect(screen.queryByRole("group", { name: "Show" })).toBeNull();
    const flat = await screen.findByRole("region", { name: "Waiting for you" });
    expect(within(flat).queryAllByRole("region")).toEqual([]);
    const rows = within(flat).getAllByTestId("inbox-row").map((r) => r.textContent);
    expect(rows).toEqual([
      expect.stringContaining("Session old"),
      expect.stringContaining("Session loose"),
      expect.stringContaining("Session trip"),
      expect.stringContaining("Anna Petrova"),
    ]);
    // the second line says where each comes from: its project, or its sphere's; nothing for one of neither
    expect(rows[0]).toMatch(/Session old.*work$/);
    expect(rows[1]).not.toContain(" · ");
    expect(rows[2]).toMatch(/Session trip.*Travel$/);
    expect(rows[3]).toContain("Travel · is a transfer needed");
    // the tasks are a tab of their own («Tasks»), never among what waits
    for (const title of ["Send Acme the draft", "Book the dentist"]) expect(screen.queryByText(title)).toBeNull();
  });

  it("names the operator's place in an old address: `?mode=operator`, `?mode=map`; an old `?mode=project` is «Today»", () => {
    expect(todaySearch({ mode: "operator" })).toEqual({ mode: "operator" });
    expect(todaySearch({ mode: "map" })).toEqual({ mode: "map" });
    expect(todaySearch({ mode: "project" })).toEqual({});
    expect(todaySearch({ mode: "action" })).toEqual({});
    expect(todaySearch({ tab: "tasks", mode: "operator" })).toEqual({ tab: "tasks" });
  });

  it("test_operator_mode_shows_only_the_sessions_requests", async () => {
    vi.spyOn(live, "watch").mockResolvedValue(undefined);
    const feed: Feed = {
      ...BY_PROJECT,
      groups: [
        {
          action: "decide",
          title: "Decide",
          rows: [row("cproposal:p1", { action: "decide", source: "collection_proposal", track: null, title: "Assistant", essence: "tasks: 1 change proposed", ref: "p1" })],
        },
        {
          action: "reply",
          title: "Reply",
          rows: [row("reply:r/1", { action: "reply", session: "r", title: "Pick a venue", essence: "Two venues hold the date", project: "work/acme" })],
        },
        { action: "read", title: "Read", rows: [row("read:old", { session: "old", title: "Session old", essence: "Done: three hotels" })] },
        ...BY_PROJECT.groups.filter((g) => g.action === "assistant"),
      ],
    };
    mount(<Operator feed={feed} spheres={SPHERES} now={NOW} />);

    const cards = await screen.findAllByTestId("operator-card");
    expect(cards.map((c) => c.getAttribute("aria-label"))).toEqual(["Pick a venue", "Session old"]); // one card each
    expect(cards[0]!.textContent).toMatch(/work \/ acme · Pick a venue · waits for your message/);
    expect(within(cards[0]!).getByText("Two venues hold the date")).toBeTruthy();
    expect(within(cards[0]!).getByRole("textbox", { name: "Your message" })).toBeTruthy();
    expect(within(cards[1]!).getByRole("link", { name: /Open the session/ }).getAttribute("href")).toBe("/s/old");
    for (const gone of ["Anna Petrova", "Assistant", "Trip to Ensk", "Send Acme the draft"]) expect(screen.queryByText(gone)).toBeNull();
    expect(screen.queryByRole("region", { name: "Working" })).toBeNull();
  });
});
