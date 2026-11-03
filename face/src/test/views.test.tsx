// A Space's views on its screen: the numbers with their goal, day and zone; «Waiting on me» with what is overdue said in
// words; a board by columns on a wide screen and one column at a time on a phone, a list when too short; a view the
// platform could not make says why; a Space without views shows its text as before. A collection read again when
// the stream says it moved on, and the whole Space when it brings a collection it did not have.
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import { type CollectionViews, forgetViews, type OneView, type SpaceViews, useSpaceViews, type View, type ViewRow } from "@/lib/collections";
import { live, type Live } from "@/lib/live";
import type { SpaceView } from "@/lib/spaces";
import type { CollectionEvent } from "@/lib/types";
import { forgetRead, SpacePage } from "@/pages/spaces";

const REV = "01J8ZQ4Y7Z5G0000000000000A";
const LATER = "01J8ZQ4Y7Z5G0000000000000B";
let n = 0;
const row = (fields: Record<string, unknown>, days: number | null, rotten = false, refs: Record<string, string> = {}): ViewRow => ({
  id: `01J8ZQ4Y7Z5G00000000000${String(++n).padStart(3, "0")}`, version: 1, fields, refs, src: null, days, rotten,
});

const STATS: View[] = [
  { name: "made", kind: "stat", title: "Follow-ups done", value: 3, goal: 10, previous: 7, window: "week", first: "2026-09-14", last: "2026-09-20" },
  { name: "revenue", kind: "stat", title: "Revenue", error: "field not found: 'amount'" },
];
const QUEUE: View = {
  name: "waiting", kind: "queue", title: "Waiting on me", rot: 2, fields: ["deal", "channel"], count: 4, dropped: 0,
  rows: [
    row({ deal: REV, channel: "mail" }, 4, true, { deal: "Initech" }),
    row({ deal: REV, channel: "phone" }, 1, false, { deal: "Globex" }),
    row({ channel: "chat" }, 0),
    row({ channel: "mail" }, -2),
  ],
};
const BOARD: View = {
  name: "pipeline", kind: "board", title: "Pipeline", group: "stage", rot: 7, fields: ["company", "value"], dropped: 0,
  columns: [
    { value: "lead", count: 1, folded: false, rows: [row({ company: "Acme", value: 12000 }, 2)] },
    { value: "qualified", count: 2, folded: false, rows: [row({ company: "Initech" }, 9, true), row({ company: "Example Ltd" }, 1)] },
    { value: "lost", count: 1, folded: true, rows: [row({ company: "Globex" }, 30)] },
  ],
};
const FEW: View = {
  name: "partners", kind: "board", title: "Partners", group: "status", rot: null, fields: ["name"], dropped: 0,
  columns: [
    { value: "talking", count: 1, folded: false, rows: [row({ name: "Example Studio" }, 5)] },
    { value: "signed", count: 1, folded: false, rows: [row({ name: "Acme Hosting" }, 40)] },
  ],
};
const EMPTY: View = { name: "open", kind: "queue", title: "Suppliers to call", rot: null, fields: ["name"], count: 0, rows: [], dropped: 0 };

const answer = (collections: CollectionViews[]): SpaceViews => ({ space: "work", tz: "Europe/Berlin", today: "2026-09-16", at: 0, collections });
const WORK = answer([
  { name: "deals", rev: REV, views: [BOARD, ...STATS] },
  { name: "followups", rev: REV, views: [QUEUE] },
  { name: "partners", rev: REV, views: [FEW] },
  { name: "suppliers", rev: null, views: [EMPTY] },
]);

const SHOWCASE: SpaceView = {
  name: "work", title: "Work", rules: "", marker: true, counts: { questions: 1, tasks: 0 },
  sections: [{ heading: "Open questions", kind: "questions", items: [{ text: "Which vendor first?", list: true }] }],
  records: { knowledge: [], profile: [], journal: [], artifacts: [] },
};

function mount(node: React.ReactNode) {
  const root = createRootRoute();
  const page = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const router = createRouter({ routeTree: root.addChildren([page]), history: createMemoryHistory({ initialEntries: ["/"] }) });
  render(<RouterProvider router={router} />);
}

const views = (space: SpaceViews | Error, one?: (c: string, v: string) => Promise<OneView>) => ({
  space: space instanceof Error ? vi.fn().mockRejectedValue(space) : vi.fn().mockResolvedValue(space),
  view: vi.fn(one ?? (() => Promise.reject(new Error("not asked")))),
});

const read = vi.fn().mockResolvedValue({ head: "h", space: SHOWCASE });

beforeEach(() => {
  forgetRead();
  forgetViews();
  live.meta = { schema: 1, owner_ok: true, face_origin: "", tracks: [], spheres: [{ name: "work", title: "Work", icon: "💼", track: "notes" }] };
});

afterEach(() => {
  vi.restoreAllMocks();
  live.stop();
  live.meta = null;
});

const section = (title: RegExp) => screen.getByRole("heading", { name: title }).closest("section") as HTMLElement;

describe("a Space's views", () => {
  it("shows the numbers beside the work, each against its goal, with the zone and the day; one not counted says so", async () => {
    mount(<SpacePage space="work" read={read} views={views(WORK)} />);
    const made = await screen.findByTestId("stat-made");
    expect(made.textContent).toBe("Follow-ups done3 / 10");
    expect(within(made).getByTitle("this week, 14 Sep–20 Sep")).toBeTruthy();
    expect(within(section(/^This week/)).getByText("14 Sep – 20 Sep")).toBeTruthy();
    expect(screen.getByTitle("field not found: 'amount'").textContent).toBe("Revenue: not counted");
  });

  it("puts a view in the tab it names: its numbers as tiles there, the first tab without them", async () => {
    const tabbed = answer([{ name: "deals", rev: REV, views: [{ ...STATS[0]!, tab: "Numbers" } as View, QUEUE] }]);
    mount(<SpacePage space="work" read={read} views={views(tabbed)} />);
    await screen.findByTestId("queue-waiting");
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Overview4", "Numbers", "Operator"]);
    expect(screen.queryByTestId("stat-made")).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "Numbers" }));
    expect((await screen.findByTestId("stat-made")).textContent).toBe("Follow-ups done3of 10this week, 14 Sep–20 Seplast week: 7");
    expect(screen.queryByTestId("queue-waiting")).toBeNull();
  });

  it("lists «Waiting on me» the longest silent first, a ref by its record's words, overdue in a word; three on a phone", async () => {
    mount(<SpacePage space="work" read={read} views={views(WORK)} />);
    const queue = await screen.findByTestId("queue-waiting");
    const rows = within(queue).getAllByRole("listitem");
    expect(rows.map((r) => r.textContent)).toEqual(["4d: 4 days, overdueInitechmail", "1d: 1 dayGlobexphone", "0d: todaychat", "in 2d: in 2 daysmail"]);
    expect(rows[3]!.className).toContain("hidden sm:block");
    fireEvent.click(screen.getByRole("button", { name: "Show all 4" }));
    expect(rows[3]!.className).not.toContain("hidden");
    expect(within(section(/^Waiting on me/)).getByText("4")).toBeTruthy();
  });

  it("says on each row which way its last touch went and when, and a row nothing names keeps the column empty", async () => {
    const talks: View = {
      name: "talks", kind: "queue", title: "In talks", mark: "wait", latest: true, rot: 7, fields: ["company"], count: 2, dropped: 0,
      rows: [
        { ...row({ company: "Initech" }, 3), latest: { date: "2026-09-14", value: "in", label: "They wrote", arrow: "in" } },
        row({ company: "Globex" }, 1),
      ],
    };
    mount(<SpacePage space="work" read={read} views={views(answer([{ name: "deals", rev: REV, views: [talks] }]))} />);
    const rows = within(await screen.findByTestId("queue-talks")).getAllByRole("listitem");
    expect(within(rows[0]!).getByTitle("They wrote · 14 Sep").textContent).toBe("14 Sep, last: They wrote · 14 Sep");
    expect(rows[1]!.textContent).toBe("1d: 1 dayGlobex");
  });

  it("leaves out a tab of queues that are all empty now, and shows it once one has a row", async () => {
    const drafts: View = { ...EMPTY, name: "drafts", title: "Drafts", tab: "Drafts", rot: 2 } as View;
    mount(<SpacePage space="work" read={read} views={views(answer([{ name: "followups", rev: REV, views: [QUEUE, drafts] }]))} />);
    await screen.findByTestId("queue-waiting");
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Overview4", "Operator"]);
    cleanup();
    forgetViews();
    const one = { ...drafts, count: 1, rows: [row({ name: "To Acme" }, 3)] } as View;
    mount(<SpacePage space="work" read={read} views={views(answer([{ name: "followups", rev: REV, views: [QUEUE, one] }]))} />);
    await screen.findByTestId("queue-waiting");
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Overview4", "Drafts1", "Operator"]);
  });

  it("shows a board on the first tab as a strip of its columns, a column's cards on a press", async () => {
    mount(<SpacePage space="work" read={read} views={views(WORK)} />);
    const strip = await screen.findByTestId("strip-pipeline");
    expect(within(strip).getAllByRole("button").map((b) => b.textContent)).toEqual(["1Lead", "2Qualified", "1Lost"]);
    fireEvent.click(within(strip).getByRole("button", { name: /Qualified/ }));
    expect(within(strip).getAllByRole("listitem").map((r) => r.textContent)).toEqual(["9d: 9 days, stuckInitech", "1d: 1 dayExample Ltd"]);
    expect(screen.queryByTestId("board-wide-pipeline")).toBeNull();
  });

  it("shows a board on its own tab by columns, the folded below; on a phone one column at a time, opened at the stuck one", async () => {
    mount(<SpacePage space="work" read={read} views={views(answer([{ name: "deals", rev: REV, views: [{ ...BOARD, tab: "Board" } as View] }]))} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Board" }));
    const wide = await screen.findByTestId("board-wide-pipeline");
    expect(within(wide).getAllByRole("heading").map((h) => h.textContent)).toEqual(["Lead (1)", "Qualified (2)"]);
    expect(within(wide).getByText("stuck · 9 days")).toBeTruthy();
    expect(within(wide).getByText("12,000")).toBeTruthy();
    expect(within(wide).getByText("Lost (1)").closest("details")).toBeTruthy();
    const phone = screen.getByTestId("board-phone-pipeline");
    const pressed = () => within(phone).getAllByRole("button").find((b) => b.getAttribute("aria-pressed") === "true")?.textContent;
    expect(pressed()).toBe("Qualified2");
    expect(within(phone).getAllByRole("listitem").map((r) => /Initech|Example Ltd/.exec(r.textContent ?? "")?.[0])).toEqual(["Initech", "Example Ltd"]);
    fireEvent.click(within(phone).getByRole("button", { name: /Lost/ }));
    expect(pressed()).toBe("Lost1");
    expect(within(phone).getByText("Globex")).toBeTruthy();
  });

  it("makes a list, not a board, of fewer than three records; an empty view says since when, not nothing", async () => {
    const tabbed = answer([{ name: "partners", rev: REV, views: [{ ...FEW, tab: "Partners" } as View] }, { name: "suppliers", rev: null, views: [EMPTY] }]);
    mount(<SpacePage space="work" read={read} views={views(tabbed)} />);
    expect(within(await screen.findByRole("region", { name: "Suppliers to call" }).catch(() => section(/^Suppliers to call/))).getByText("No data yet: nothing was written here.")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Partners" }));
    const list = await screen.findByTestId("board-list-partners");
    expect(within(list).getAllByRole("listitem").map((r) => r.textContent)).toEqual(["5d: 5 daysExample StudioTalking", "40d: 40 daysAcme HostingSigned"]);
    expect(screen.queryByTestId("board-wide-partners")).toBeNull();
  });

  it("leaves a Space without views as it was, and one whose platform has no views route", async () => {
    for (const given of [answer([]), new ApiError(404, "Not found.")]) {
      forgetViews();
      const api = views(given);
      mount(<SpacePage space="work" read={read} views={api} />);
      await screen.findByText("Which vendor first?");
      await waitFor(() => expect(api.space).toHaveBeenCalledWith("work"));
      expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Overview", "Operator"]);
      expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual(["Yours", "Sessions"]);
      cleanup();
    }
  });

  it("says when the views did not load", async () => {
    mount(<SpacePage space="work" read={read} views={views(new ApiError(503, "The collections are busy: try again."))} />);
    expect(await screen.findByText("The views did not load: The collections are busy: try again.")).toBeTruthy();
  });
});

describe("useSpaceViews", () => {
  function stream() {
    const told = new Set<(e: CollectionEvent) => void>();
    const resynced = new Set<() => void>();
    const source = {
      onCollection: (l: (e: CollectionEvent) => void) => {
        told.add(l);
        return () => told.delete(l);
      },
      onResync: (l: () => void) => {
        resynced.add(l);
        return () => resynced.delete(l);
      },
    } as unknown as Live;
    const tell = (e: CollectionEvent) => {
      for (const l of told) l(e);
    };
    const resync = () => {
      for (const l of resynced) l();
    };
    return { source, tell, resync };
  }

  it("reads a collection's views again when its rev moves on, and only then", async () => {
    const moved: View = { ...QUEUE, count: 3, rows: QUEUE.rows.slice(1) };
    const api = views(answer([{ name: "followups", rev: REV, views: [QUEUE] }]), async (name, view) => ({
      name, rev: LATER, tz: "Europe/Berlin", today: "2026-09-17", at: 1, view: view === "waiting" ? moved : QUEUE,
    }));
    const { source, tell } = stream();
    const { result } = renderHook(() => useSpaceViews("work", api, source));
    await waitFor(() => expect(result.current.data?.collections[0]?.rev).toBe(REV));
    await act(async () => tell({ name: "followups", space: "work", rev: REV }));
    expect(api.view).not.toHaveBeenCalled();
    await act(async () => tell({ name: "followups", space: "work", rev: LATER }));
    await waitFor(() => expect(result.current.data?.collections[0]?.rev).toBe(LATER));
    expect(api.view).toHaveBeenCalledWith("followups", "waiting");
    expect(result.current.data?.collections[0]?.views[0]).toEqual(moved);
    expect(result.current.data?.today).toBe("2026-09-17");
    expect(api.space).toHaveBeenCalledTimes(1);
  });

  it("reads the Space anew for a collection come into it, a view gone, and after a resync — not for another Space's", async () => {
    const api = views(answer([{ name: "followups", rev: REV, views: [QUEUE] }]), () => Promise.reject(new ApiError(404, "No such view or record.")));
    const { source, tell, resync } = stream();
    const { result } = renderHook(() => useSpaceViews("work", api, source));
    await waitFor(() => expect(result.current.data).not.toBeNull());
    await act(async () => tell({ name: "trips", space: "travel", rev: LATER }));
    expect(api.space).toHaveBeenCalledTimes(1);
    await act(async () => tell({ name: "deals", space: "work", rev: LATER }));
    await waitFor(() => expect(api.space).toHaveBeenCalledTimes(2));
    await act(async () => tell({ name: "followups", space: "work", rev: LATER }));
    await waitFor(() => expect(api.space).toHaveBeenCalledTimes(3));
    await act(async () => resync());
    await waitFor(() => expect(api.space).toHaveBeenCalledTimes(4));
  });
});
