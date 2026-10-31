// A Space's views on its screen: the numbers with their goal, day and zone; «My move» with what is overdue said in
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
  { name: "made", kind: "stat", title: "Follow-ups sent myself", value: 3, goal: 10, previous: 7, window: "week", first: "2026-09-14", last: "2026-09-20" },
  { name: "revenue", kind: "stat", title: "Revenue", error: "field not found: 'amount'" },
];
const QUEUE: View = {
  name: "my_move", kind: "queue", title: "My move", rot: 2, fields: ["deal", "channel"], count: 4, dropped: 0,
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
  it("shows the numbers with their goal, the zone and the day; one not counted says why", async () => {
    mount(<SpacePage space="work" read={read} views={views(WORK)} />);
    const tile = await screen.findByTestId("stat-made");
    expect(tile.textContent).toBe("Follow-ups sent myself3of 10this week, 14.09–20.09last week: 7");
    expect(within(tile).getByRole("img").getAttribute("aria-label")).toBe("3 of 10");
    expect(within(section(/^Numbers/)).getByText("as of 16.09 · Europe/Berlin")).toBeTruthy();
    expect(screen.getByText("Not counted: field not found: 'amount'")).toBeTruthy();
  });

  it("lists «My move» the longest silent first, a ref by its record's words, overdue in a word; three on a phone", async () => {
    mount(<SpacePage space="work" read={read} views={views(WORK)} />);
    const queue = await screen.findByTestId("queue-my_move");
    const rows = within(queue).getAllByRole("listitem");
    expect(rows.map((r) => r.textContent)).toEqual(["Initechmailoverdue · 4 days", "Globexphone1 day", "chattoday", "mailin 2 days"]);
    expect(rows[3]!.className).toContain("hidden sm:block");
    fireEvent.click(screen.getByRole("button", { name: "Show all 4" }));
    expect(rows[3]!.className).not.toContain("hidden");
    expect(within(section(/^My move/)).getByText("(4)")).toBeTruthy();
  });

  it("shows a board by columns, the folded below; on a phone one column at a time, opened at the stuck one", async () => {
    mount(<SpacePage space="work" read={read} views={views(WORK)} />);
    const wide = await screen.findByTestId("board-wide-pipeline");
    expect(within(wide).getAllByRole("heading").map((h) => h.textContent)).toEqual(["Lead (1)", "Qualified (2)"]);
    expect(within(wide).getByText("stuck · 9 days")).toBeTruthy();
    expect(within(wide).getByText("12,000")).toBeTruthy();
    expect(within(wide).getByText("Lost (1)").closest("details")).toBeTruthy();
    const phone = screen.getByTestId("board-phone-pipeline");
    const pressed = () => within(phone).getAllByRole("button").find((b) => b.getAttribute("aria-pressed") === "true")?.textContent;
    expect(pressed()).toBe("Qualified2");
    expect(within(phone).getAllByRole("listitem").map((r) => r.textContent?.split(/stuck|\d/)[0])).toEqual(["Initech", "Example Ltd"]);
    fireEvent.click(within(phone).getByRole("button", { name: /Lost/ }));
    expect(pressed()).toBe("Lost1");
    expect(within(phone).getByText("Globex")).toBeTruthy();
  });

  it("makes a list, not a board, of fewer than three records; an empty view says since when, not nothing", async () => {
    mount(<SpacePage space="work" read={read} views={views(WORK)} />);
    const list = await screen.findByTestId("board-list-partners");
    expect(within(list).getAllByRole("listitem").map((r) => r.textContent)).toEqual(["Example StudioTalking5 days", "Acme HostingSigned40 days"]);
    expect(screen.queryByTestId("board-wide-partners")).toBeNull();
    expect(within(section(/^Suppliers to call/)).getByText("No data yet: nothing was written here.")).toBeTruthy();
  });

  it("leaves a Space without views as it was, and one whose platform has no views route", async () => {
    for (const given of [answer([]), new ApiError(404, "Not found.")]) {
      forgetViews();
      const api = views(given);
      mount(<SpacePage space="work" read={read} views={api} />);
      await screen.findByText("Which vendor first?");
      await waitFor(() => expect(api.space).toHaveBeenCalledWith("work"));
      expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual(["Sessions (0)", "Open questions (1)", "Tasks (0)", "Records (0)"]);
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
      name, rev: LATER, tz: "Europe/Berlin", today: "2026-09-17", at: 1, view: view === "my_move" ? moved : QUEUE,
    }));
    const { source, tell } = stream();
    const { result } = renderHook(() => useSpaceViews("work", api, source));
    await waitFor(() => expect(result.current.data?.collections[0]?.rev).toBe(REV));
    await act(async () => tell({ name: "followups", rev: REV }));
    expect(api.view).not.toHaveBeenCalled();
    await act(async () => tell({ name: "followups", rev: LATER }));
    await waitFor(() => expect(result.current.data?.collections[0]?.rev).toBe(LATER));
    expect(api.view).toHaveBeenCalledWith("followups", "my_move");
    expect(result.current.data?.collections[0]?.views[0]).toEqual(moved);
    expect(result.current.data?.today).toBe("2026-09-17");
    expect(api.space).toHaveBeenCalledTimes(1);
  });

  it("reads the Space anew for a collection it did not have, a view gone, and after a resync", async () => {
    const api = views(answer([{ name: "followups", rev: REV, views: [QUEUE] }]), () => Promise.reject(new ApiError(404, "No such view or record.")));
    const { source, tell, resync } = stream();
    const { result } = renderHook(() => useSpaceViews("work", api, source));
    await waitFor(() => expect(result.current.data).not.toBeNull());
    await act(async () => tell({ name: "deals", rev: LATER }));
    await waitFor(() => expect(api.space).toHaveBeenCalledTimes(2));
    await act(async () => tell({ name: "followups", rev: LATER }));
    await waitFor(() => expect(api.space).toHaveBeenCalledTimes(3));
    await act(async () => resync());
    await waitFor(() => expect(api.space).toHaveBeenCalledTimes(4));
  });
});
