import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TABBED } from "@/components/shell";
import { feedSource } from "@/lib/feed";
import { keyboardUp } from "@/lib/keyboard";
import { live } from "@/lib/live";
import { parsePane, paneValue, recordHref, splitRecord } from "@/lib/pane";
import { spacesApi } from "@/lib/spaces";
import { makeRouter } from "@/router";

function phone(coarse: boolean) {
  // Below 1024 px: the phone's layout; `pointer: coarse` when it is a touch screen.
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query.includes("pointer: coarse") ? coarse : false,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
}

async function open(path: string) {
  const router = makeRouter(createMemoryHistory({ initialEntries: [path] }));
  render(<RouterProvider router={router} />);
  await act(async () => {
    await router.load();
  });
  return router;
}

beforeEach(() => {
  vi.spyOn(live, "start").mockResolvedValue(undefined);
  vi.spyOn(live, "watch").mockResolvedValue(undefined);
  vi.spyOn(feedSource, "start").mockImplementation(() => undefined);
  live.meta = {
    schema: 1, owner_ok: true, face_origin: "", tracks: [{ name: "notes", lane: "private", kind: "private" }],
    spheres: [{ name: "travel", title: "Путешествия", icon: "✈️", track: "notes" }],
  };
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  live.stop();
  live.meta = null;
  feedSource.feed = null;
});

describe("the routes", () => {
  it("start at «Сегодня»: `/` goes there with its hash (a Mini App's signed data rides in it)", async () => {
    phone(false);
    const router = await open("/#tgWebAppData=abc");
    await waitFor(() => expect(router.state.location.pathname).toBe("/today"));
    expect(router.state.location.hash).toBe("tgWebAppData=abc");
    expect(await screen.findByRole("heading", { name: /^Сегодня/ })).toBeTruthy();
  });

  it("keep the old links: a session, a card, a file, a commit card, a push card", async () => {
    phone(false);
    for (const path of ["/s/a1b2c3d4", "/a/5f3a9c21", "/f/f1", "/b/b1", "/p/p1"]) {
      const router = await open(path);
      expect(router.state.matches.at(-1)?.routeId).not.toBe("__root__");
      expect(router.state.location.pathname).toBe(path);
    }
  });

  it("open the new tabs, and a Record by its path inside the Space", async () => {
    phone(false);
    const record = vi.spyOn(spacesApi, "record").mockReturnValue(new Promise(() => undefined));
    const router = await open("/spaces/travel/r/knowledge/ensk-transit.md");
    expect(router.state.matches.at(-1)?.params).toMatchObject({ space: "travel", _splat: "knowledge/ensk-transit.md" });
    await waitFor(() => expect(record).toHaveBeenCalledWith("travel", "knowledge/ensk-transit.md"));
  });
});

describe("the tab bar", () => {
  it("stands under the tabs with the waiting count, not under a session; «＋» opens the new task in place", async () => {
    phone(false);
    feedSource.feed = { schema: 1, count: 3, groups: [], bot: "", assistant_at: null };
    const router = await open("/today");
    const bar = await screen.findByRole("navigation", { name: "Разделы" });
    expect(bar.textContent).toContain("Сегодня");
    expect(screen.getByLabelText("ждут: 3")).toBeTruthy();
    expect(within(bar).getByRole("link", { name: /Сегодня/ }).getAttribute("aria-current")).toBe("page");

    fireEvent.click(within(bar).getByRole("link", { name: "Новая задача" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/new"));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(await screen.findByRole("textbox", { name: "Что сделать" })).toBeTruthy();
    const newBar = screen.getByRole("navigation", { name: "Разделы" });
    expect(within(newBar).getByRole("link", { name: "Новая задача" }).getAttribute("aria-current")).toBe("page");

    await act(async () => {
      await router.navigate({ to: "/s/$id", params: { id: "a1b2c3d4" } });
    });
    expect(screen.queryByRole("navigation", { name: "Разделы" })).toBeNull();
  });

  it("steps aside while the keyboard is up, and only then: not for a focus alone, not for a pinch zoom", async () => {
    phone(true);
    const viewport = Object.assign(new EventTarget(), { height: 844, scale: 1 });
    vi.stubGlobal("visualViewport", viewport);
    vi.stubGlobal("innerHeight", 844);
    const bar = () => screen.queryByRole("navigation", { name: "Разделы" });
    const resize = (height: number, scale = 1) =>
      act(() => {
        Object.assign(viewport, { height, scale });
        viewport.dispatchEvent(new Event("resize"));
      });
    await open("/sessions");
    expect(await screen.findByRole("navigation", { name: "Разделы" })).toBeTruthy();
    const field = document.createElement("input");
    document.body.append(field);
    act(() => field.focus());
    expect(bar()).toBeTruthy(); // focused, no keyboard yet
    resize(500);
    await waitFor(() => expect(bar()).toBeNull()); // the keyboard took the bottom
    resize(844);
    await waitFor(() => expect(bar()).toBeTruthy()); // closed by the back gesture: the field keeps the focus
    resize(400, 2);
    expect(bar()).toBeTruthy(); // a pinch zoom is not a keyboard
    resize(500);
    await waitFor(() => expect(bar()).toBeNull());
    act(() => field.blur());
    await waitFor(() => expect(bar()).toBeTruthy());
    field.remove();
  });

  it("reads the keyboard by the rule", () => {
    expect(keyboardUp(true, 1, 500, 844)).toBe(true);
    expect(keyboardUp(false, 1, 500, 844)).toBe(false);
    expect(keyboardUp(true, 1.8, 500, 844)).toBe(false);
    expect(keyboardUp(true, 1, 800, 844)).toBe(false);
  });

  it("scrolls the page itself, so a page that just grows never spills past the bar", async () => {
    phone(false);
    await open("/spaces");
    const page = await screen.findByRole("heading", { name: "Пространства" });
    const scroller = screen.getByRole("navigation", { name: "Разделы" }).previousElementSibling as HTMLElement;
    expect(scroller.contains(page) && scroller.className).toMatch(/min-h-0.*overflow-y-auto/);
  });

  it("is on exactly the tabs' screens", () => {
    const on = ["/today", "/sessions", "/new", "/read", "/spaces", "/spaces/travel", "/spaces/travel/r/a.md"];
    const off = ["/s/a1b2c3d4", "/a/x", "/login", "/f/x", "/sessionsx"];
    expect(on.every((p) => TABBED.test(p))).toBe(true);
    expect(off.some((p) => TABBED.test(p))).toBe(false);
  });
});

describe("the right pane", () => {
  it("reads only what it can open", () => {
    expect(parsePane("r:travel/knowledge/ensk.md")).toEqual({ kind: "record", space: "travel", path: "knowledge/ensk.md" });
    expect(parsePane("d:a1b2c3d4")).toEqual({ kind: "diff", session: "a1b2c3d4" });
    expect(parsePane("f:f_1")).toEqual({ kind: "file", id: "f_1" });
    expect(parsePane("r:work/profile/acme/2026-10-01-x.v2.md")).toMatchObject({ path: "profile/acme/2026-10-01-x.v2.md" });
    for (const bad of ["r:travel/../x.md", "r:travel/.scratch/x.md", "r:Travel/x.md", "r:travel/", "d:a/b", "x:1", 5, undefined,
      "r:travel/x.txt", "r:travel/a b.md", "r:travel/a?.md", "r:travel/a#b.md", "r:travel/a%2e.md", "r:travel/a/b/c/d.md",
      "r:travel/зурбаган.md", "r:travel/.md"])
      expect(parsePane(bad)).toBeNull();
    expect(paneValue({ kind: "record", space: "travel", path: "a/b.md" })).toBe("r:travel/a/b.md");
  });

  it("a Record's page and a path of the base", () => {
    expect(recordHref("travel", "knowledge/a b.md")).toBe("/spaces/travel/r/knowledge/a%20b.md");
    expect(splitRecord("personal/profile/pass.md")).toEqual({ space: "personal", path: "profile/pass.md" });
    expect(splitRecord("pass.md")).toBeNull();
  });

  it("stays open while a session's tabs change", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("min-width: 1024px"),
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }));
    live.dispatch({ type: "list", snapshot: { schema: 1, cursor: 1, sessions: [
      { id: "a1b2c3d4", track: "retinue", lane: "code", kind: "code", state: "idle", created: 1, at: 1, title: "Код", waiting: 0 },
    ] } });
    const router = await open("/s/a1b2c3d4?pane=r:travel/a.md");
    fireEvent.click(await screen.findByRole("tab", { name: "Изменения" }));
    await waitFor(() => expect(router.state.location.search).toMatchObject({ tab: "changes", pane: "r:travel/a.md" }));
    expect(screen.getByRole("complementary", { name: "Запись" })).toBeTruthy();
  });

  it("opens a Record beside the page on a wide screen", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("min-width"),
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }));
    const record = vi.spyOn(spacesApi, "record").mockReturnValue(new Promise(() => undefined));
    await open("/today?pane=r:travel/knowledge/ensk.md");
    await screen.findByRole("complementary", { name: "Запись" });
    expect(document.querySelector("main.overflow-y-auto.min-h-0")?.textContent).toContain("Сегодня"); // the middle scrolls
    await waitFor(() => expect(record).toHaveBeenCalledWith("travel", "knowledge/ensk.md")); // B's RecordPage, in the pane
    expect(screen.getByRole("navigation", { name: "Пространства" }).textContent).toContain("✈️ Путешествия");
    fireEvent.click(screen.getByRole("button", { name: "Закрыть" }));
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "Запись" })).toBeNull());
  });
});
