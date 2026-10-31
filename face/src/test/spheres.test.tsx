import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionList } from "@/components/session-list";
import { SessionMenu } from "@/components/session-menu";
import { live } from "@/lib/live";
import { useMarkSeen } from "@/lib/seen";
import { foldRows } from "@/lib/spheres";
import type { Session } from "@/lib/types";

const session = (id: string, over: Partial<Session> = {}): Session => ({
  id, track: "notes", lane: "private", kind: "private", state: "idle", created: 1, at: 1, title: `Session ${id}`,
  waiting: 0, ...over,
});

function mount(node: React.ReactNode) {
  const root = createRootRoute();
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>session opened</p> });
  const router = createRouter({
    routeTree: root.addChildren([home, one]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  return render(<RouterProvider router={router} />);
}

beforeEach(() => {
  live.meta = {
    schema: 1, owner_ok: true, face_origin: "", tracks: [{ name: "notes", lane: "private", kind: "private" }],
    spheres: [
      { name: "travel", title: "Travel", icon: "✈️", track: "notes" },
      { name: "health", title: "Health", icon: "", track: "notes" },
    ],
  };
});

afterEach(() => {
  vi.restoreAllMocks();
  live.stop();
  live.meta = null;
});

describe("SessionList by sphere", () => {
  it("groups by sphere, the main one first and starred, a fork indented, unread marked, a «＋» per sphere", async () => {
    live.dispatch({
      type: "list",
      snapshot: {
        schema: 1, cursor: 5, sessions: [
          session("m1", { sphere: "travel", main: true, at: 1 }),
          session("o1", { sphere: "travel", at: 9, unread: true }),
          session("f1", { sphere: "travel", at: 5, fork_of: "m1", title: "by train" }),
          session("x1", { at: 3 }),
        ],
      },
    });
    mount(<SessionList />);
    const travel = (await screen.findByRole("heading", { name: /✈️ Travel/ })).closest("section")!;
    const links = within(travel as HTMLElement).getAllByRole("link");
    expect(links.map((a) => a.textContent)).toEqual([
      expect.stringContaining("main: Session m1"),
      expect.stringContaining("fork: by train"),
      expect.stringContaining("Session o1"),
    ]);
    expect((links[1]!.closest("li") as HTMLElement).style.marginLeft).toBe("1.25rem");
    expect(within(links[2]!).getByRole("img", { name: "unread" })).toBeTruthy();
    expect(within(links[0]!).queryByRole("img", { name: "unread" })).toBeNull();
    expect(screen.getByRole("heading", { name: /No sphere/ })).toBeTruthy();
    fireEvent.click(within(travel as HTMLElement).getByRole("button", { name: "New session: ✈️ Travel" }));
    const dialog = await screen.findByRole("dialog", { name: "New session" });
    expect(within(dialog).getByRole("radio", { name: "✈️ Travel" })).toHaveProperty("checked", true);
  });
});

describe("sub-sessions, as Claude Code's sidebar has them", () => {
  afterEach(() => localStorage.clear());

  function seedTravel() {
    live.dispatch({
      type: "list",
      snapshot: {
        schema: 1, cursor: 5, sessions: [
          session("m1", { sphere: "travel", main: true, at: 1, title: "Travel", children: 2 }),
          session("c1", { sphere: "travel", at: 7, sub_of: "m1", title: "lodging", state: "waiting", waiting: 1 }),
          session("c2", { sphere: "travel", at: 8, sub_of: "m1", side: false, title: "trip to the sea" }),
          session("q1", { sphere: "travel", at: 3, parent: "o1", side: true, title: "why so?" }),
          session("q2", { sphere: "travel", at: 2, parent: "o1", title: "and this?" }), // a platform before `side`
          session("o1", { sphere: "travel", at: 9, title: "tickets for May" }),
          session("h1", { sphere: "health", at: 2 }),
        ],
      },
    });
  }
  const section = async () => (await screen.findByRole("heading", { name: /✈️ Travel/ })).closest("section") as HTMLElement;
  const titlesIn = (el: HTMLElement) => within(el).queryAllByRole("link").map((a) => a.textContent ?? "");

  it("nest under the session that started them (`sub_of`), side questions under theirs; folds are remembered", async () => {
    seedTravel();
    const view = mount(<SessionList />);
    const travel = await section();
    const titles = () => titlesIn(travel);
    expect(titles()).toEqual([
      expect.stringContaining("main: Travel"),
      expect.stringContaining("trip to the sea"),
      expect.stringContaining("lodging"),
      expect.stringContaining("tickets for May"),
      expect.stringContaining("side question: why so?"),
      expect.stringContaining("side question: and this?"),
    ]);
    expect(titles()[1]).not.toContain("side question:");
    expect((within(travel).getAllByRole("link")[1]!.closest("li") as HTMLElement).style.marginLeft).toBe("1.25rem");
    expect(within(travel).getByRole("button", { name: "Collapse nested: Travel" }).getAttribute("title")).toBe(
      "Collapse nested: 2 · sub-sessions open: 2",
    );

    // 44 px to tap on a touch screen, the heading and the fold alike; small to look at with a mouse
    expect(within(travel).getByRole("button", { name: /^✈️ Travel/ }).className).toContain("pointer-coarse:min-h-11");
    expect(within(travel).getByRole("button", { name: "Collapse nested: Travel" }).className).toMatch(
      /pointer-coarse:h-11 pointer-coarse:min-w-11/,
    );
    fireEvent.click(within(travel).getByRole("button", { name: "Collapse nested: Travel" }));
    expect(titles()).toEqual([
      expect.stringContaining("Travel"), expect.stringContaining("tickets for May"),
      expect.stringContaining("why so?"), expect.stringContaining("and this?"),
    ]);
    // the folded sub-session that waits on the owner is said on the fold
    expect(within(travel).getByRole("button", { name: "Expand: Travel (2, 1 waiting)" }).getAttribute("aria-expanded"))
      .toBe("false");

    const header = within(travel).getByRole("button", { name: /^✈️ Travel/ });
    fireEvent.click(header);
    expect(header.getAttribute("aria-expanded")).toBe("false");
    expect(titles()).toEqual([]);
    expect(within(header).getByLabelText("1 waiting").textContent).toBe("1 waiting");
    expect(screen.getByRole("heading", { name: /Health/ })).toBeTruthy(); // the others stay open

    view.unmount();
    mount(<SessionList />);
    const again = await section();
    expect(within(again).queryAllByRole("link")).toHaveLength(0);
    fireEvent.click(within(again).getByRole("button", { name: /^✈️ Travel/ }));
    expect(within(again).getAllByRole("link")).toHaveLength(4); // the main one is still folded
  });

  it("under «Waiting» nothing folded hides what waits", async () => {
    localStorage.setItem("retinue-list-folded", JSON.stringify(["m1", "sphere:travel"]));
    seedTravel();
    mount(<SessionList />);
    const travel = await section();
    expect(titlesIn(travel)).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Waiting" }));
    await waitFor(() => expect(titlesIn(travel)).toEqual([expect.stringContaining("lodging")]));
    fireEvent.click(screen.getByRole("button", { name: "All" }));
  });

  it("never hides the session on screen: its sphere and its parent stay open", async () => {
    localStorage.setItem("retinue-list-folded", JSON.stringify(["m1", "sphere:travel"]));
    seedTravel();
    mount(<SessionList current="c2" />);
    const travel = await section();
    expect(titlesIn(travel).slice(0, 3)).toEqual([
      expect.stringContaining("Travel"), expect.stringContaining("trip to the sea"), expect.stringContaining("lodging"),
    ]);
  });

  it("folds by depth: a folded row hides what hangs under it, not its siblings, nor the current one", () => {
    const row = (id: string, depth: number, over: Partial<Session> = {}) => ({ session: session(id, over), depth, side: false });
    const rows = [row("a", 0), row("b", 1), row("c", 2, { state: "waiting" }), row("d", 1, { waiting: 2 }), row("e", 0)];
    expect(foldRows(rows, new Set()).map((r) => [r.session.id, r.kids, r.folded])).toEqual([
      ["a", 3, false], ["b", 1, false], ["c", 0, false], ["d", 0, false], ["e", 0, false],
    ]);
    expect(foldRows(rows, new Set(["b"])).map((r) => r.session.id)).toEqual(["a", "b", "d", "e"]);
    expect(foldRows(rows, new Set(["a", "e"])).map((r) => [r.session.id, r.folded, r.waiting])).toEqual([["a", true, 2], ["e", false, 0]]);
    expect(foldRows(rows, new Set(["a", "b"]), "c").map((r) => r.session.id)).toEqual(["a", "b", "c", "d", "e"]);
  });
});

describe("SessionMenu", () => {
  it("moves the session to a sphere, marks it main and forks it with a first message", async () => {
    const s = session("a1b2c3d4", { sphere: "travel" });
    const calls = {
      setSphere: vi.fn().mockResolvedValue({ session: { ...s, sphere: "health" } }),
      setMain: vi.fn().mockResolvedValue({ session: { ...s, main: true } }),
      fork: vi.fn().mockResolvedValue({ session: "77889900", say: "ok" }),
    };
    mount(<SessionMenu s={s} calls={calls} />);
    fireEvent.click(await screen.findByRole("button", { name: "Menu: Session a1b2c3d4" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("button", { name: "✈️ Travel" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(within(dialog).getByRole("button", { name: "Health" }));
    await waitFor(() => expect(calls.setSphere).toHaveBeenCalledWith("a1b2c3d4", "health"));
    expect(live.getState().sessions.a1b2c3d4?.sphere).toBe("health");
    fireEvent.click(within(dialog).getByRole("button", { name: "Make main in the sphere" }));
    await waitFor(() => expect(calls.setMain).toHaveBeenCalledWith("a1b2c3d4", true));
    fireEvent.click(within(dialog).getByRole("button", { name: "Fork…" }));
    fireEvent.change(screen.getByRole("textbox", { name: "First message" }), { target: { value: " by train? " } });
    fireEvent.click(screen.getByRole("button", { name: "Fork" }));
    expect(await screen.findByText("session opened")).toBeTruthy();
    expect(calls.fork).toHaveBeenCalledWith("a1b2c3d4", "by train?", expect.stringMatching(/^face:/));
  });

  it("offers no fork for a side question", async () => {
    mount(<SessionMenu s={session("side", { parent: "a1b2c3d4" })} />);
    fireEvent.click(await screen.findByRole("button", { name: "Menu: Session side" }));
    expect(screen.queryByRole("button", { name: "Fork…" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Make main in the sphere" })).toBeNull();
  });
});

describe("useMarkSeen", () => {
  function Seen({ unread, seen }: { unread: boolean; seen: () => Promise<{ session: Session }> }) {
    useMarkSeen("a1b2c3d4", unread, seen);
    return null;
  }

  it("tells the platform once the session is unread on a visible screen, not while the app is hidden", async () => {
    const seen = vi.fn().mockResolvedValue({ session: session("a1b2c3d4") });
    const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    const view = render(<Seen unread seen={seen} />);
    expect(seen).not.toHaveBeenCalled();
    hidden.mockReturnValue(false);
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await waitFor(() => expect(seen).toHaveBeenCalledTimes(1));
    view.rerender(<Seen unread={false} seen={seen} />);
    expect(seen).toHaveBeenCalledTimes(1);
  });
});
