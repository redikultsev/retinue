// The operator's map (lib/map.ts, components/map.tsx): every live session in its project's area at the place the
// platform gave it (`slot`), kept while its state changes; a tile opens its card in the pane beside, the map staying
// put; the arrows go from tile to tile and Esc closes the card.
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MapView } from "@/components/map";
import type { Feed, FeedRow } from "@/lib/feed";
import { live } from "@/lib/live";
import { mapAreas, placeAll } from "@/lib/map";
import { NUMBERS_KEY } from "@/lib/operator";
import { forgetProjects } from "@/lib/projects";
import type { Session } from "@/lib/types";

const NOW = 1_000_000;
const SPHERES = [
  { name: "travel", title: "Travel", icon: "", track: "notes" },
  { name: "work", title: "Work", icon: "", track: "notes" },
];

const session = (id: string, over: Partial<Session> = {}): Session => ({
  id, track: "notes", lane: "private", kind: "private", state: "idle", created: 1, at: NOW, title: `Session ${id}`, waiting: 0, ...over,
});

const row = (key: string, over: Partial<FeedRow> = {}): FeedRow =>
  ({
    key, action: "reply", source: "session", session: key, track: "notes", sphere: null, sphere_title: null, sphere_icon: null,
    project: null, title: `Session ${key}`, since: NOW - 600, essence: "which host", ref: key, ...over,
  }) as unknown as FeedRow;

function mount(node: React.ReactNode) {
  const root = createRootRoute();
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>session opened</p> });
  const router = createRouter({ routeTree: root.addChildren([home, one]), history: createMemoryHistory({ initialEntries: ["/"] }) });
  return render(<RouterProvider router={router} />);
}

const seed = (sessions: Session[]) => live.dispatch({ type: "list", snapshot: { schema: 1, cursor: 1, sessions } });
const order = () => screen.getAllByTestId("map-tile").map((t) => t.getAttribute("data-session"));

beforeEach(() => {
  live.meta = { schema: 1, owner_ok: true, face_origin: "", tracks: [], spheres: SPHERES };
  vi.spyOn(live, "watch").mockResolvedValue(undefined);
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("min-width"), addEventListener() {}, removeEventListener() {} }));
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  live.stop();
  live.meta = null;
  localStorage.clear();
  forgetProjects();
});

describe("the map's places", () => {
  it("puts each session at its slot, a place taken twice at the next free one, one from before the slots after them", () => {
    const { places, next } = placeAll([
      session("c", { slot: 3 }),
      session("a", { slot: 0 }),
      session("old", { created: 0 }),
      session("b", { slot: 0, created: 2 }),
    ]);
    expect(places.map((s) => s?.id ?? null)).toEqual(["a", "b", null, "c", "old"]);
    expect(next).toBe(2);
    expect(placeAll([session("a", { slot: 0 })]).next).toBe(1);
  });

  it("orders the areas as the left panel does: the spheres first, a sub-project right after its project, «Other» last", () => {
    const areas = mapAreas(
      [session("w", { sphere: "work" }), session("o"), session("l", { project: "travel/lisbon" }), session("z", { project: "zoo" })],
      SPHERES,
      [{ name: "travel", title: "Travel", parent: "" }, { name: "travel/lisbon", title: "Lisbon in May", parent: "travel" }, { name: "zoo", title: "Zoo", parent: "" }],
      (s) => (s.project ?? s.sphere ?? ""),
    );
    expect(areas.map((a) => [a.title, a.depth])).toEqual([["Travel", 0], ["Lisbon in May", 1], ["Work", 0], ["Zoo", 0], ["Other", 0]]);
  });
});

describe("MapView", () => {
  it("test_map_keeps_each_session_in_its_slot_when_states_change", async () => {
    seed([
      session("a", { sphere: "travel", slot: 0, state: "active" }),
      session("b", { sphere: "travel", slot: 1 }),
      session("c", { sphere: "travel", slot: 2, state: "waiting", waiting: 1 }),
      session("w", { sphere: "work", slot: 0 }),
    ]);
    mount(<MapView feed={null} spheres={SPHERES} now={NOW} />);
    const travel = await screen.findByRole("region", { name: "Travel" });
    expect(order()).toEqual(["a", "b", "c", "w"]);
    expect(within(travel).getAllByTestId("map-next")).toHaveLength(1); // where the next one appears
    expect(within(travel).getByRole("button", { name: /Session c, waits for you/ })).toBeTruthy();
    // every state changes; no tile moves
    act(() => {
      seed([
        session("a", { sphere: "travel", slot: 0, state: "waiting", waiting: 1 }),
        session("b", { sphere: "travel", slot: 1, state: "active" }),
        session("c", { sphere: "travel", slot: 2 }),
        session("w", { sphere: "work", slot: 0, state: "active" }),
      ]);
    });
    expect(order()).toEqual(["a", "b", "c", "w"]);
    expect(within(travel).getByRole("button", { name: /Session a, waits for you/ })).toBeTruthy();
    expect(within(travel).getByRole("button", { name: /Session c, done/ }).className).toContain("opacity-60");
    // one archived leaves its place free; the others stay where they were
    act(() => {
      seed([
        session("a", { sphere: "travel", slot: 0 }),
        session("c", { sphere: "travel", slot: 2 }),
        session("w", { sphere: "work", slot: 0 }),
      ]);
    });
    expect(order()).toEqual(["a", "c", "w"]);
    const cells = [...(travel.querySelector(".grid")?.children ?? [])];
    expect(cells.map((c) => c.getAttribute("data-session") ?? c.getAttribute("data-testid") ?? "")).toEqual(["a", "map-next", "c"]);
  });

  it("test_map_tile_opens_its_card_beside_and_the_map_stays_put", async () => {
    localStorage.setItem(NUMBERS_KEY, JSON.stringify({ c: { n: 3 } }));
    seed([session("a", { sphere: "travel", slot: 0, state: "active" }), session("c", { sphere: "travel", slot: 1, title: "A host for the site" })]);
    const feed: Feed = { schema: 1, count: 1, bot: "", assistant_at: null, deadlines: [], chips: [], groups: [{ action: "reply", title: "Reply", rows: [row("c", { title: "A host for the site" })] }] };
    mount(<MapView feed={feed} spheres={SPHERES} now={NOW} />);
    const tile = await screen.findByRole("button", { name: "#3 A host for the site, waits for you" });
    expect(tile.textContent).toContain("Reply: which host");
    const before = order();
    const pane = screen.getByRole("complementary", { name: "The card" });
    expect(within(pane).getByText("Press a tile to open its card here")).toBeTruthy();
    fireEvent.click(tile);
    const card = within(pane).getByTestId("operator-card");
    expect(card.getAttribute("aria-label")).toBe("A host for the site");
    expect(within(card).getByText("#3")).toBeTruthy();
    expect(within(card).getByRole("textbox", { name: "Your message" })).toBeTruthy();
    expect(within(card).getByRole("button", { name: "Dictate the answer" })).toBeTruthy();
    expect(order(), "the map stays put").toEqual(before);
    expect(tile.getAttribute("aria-pressed")).toBe("true");
    // the arrows go from tile to tile; Esc closes the card
    const first = screen.getByRole("button", { name: /Session a, working/ });
    first.focus();
    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(document.activeElement).toBe(tile);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(within(pane).queryByTestId("operator-card")).toBeNull();
  });
});
