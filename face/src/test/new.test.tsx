import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { feedSource } from "@/lib/feed";
import { live } from "@/lib/live";
import type { Session } from "@/lib/types";
import { generalTrack, greeting, NO_TRACK, startNew, statusLine } from "@/pages/new";
import { makeRouter } from "@/router";
import { mountNew } from "./mount-new";

const session = (id: string, over: Partial<Session> = {}): Session => ({
  id, track: "notes", lane: "private", kind: "private", state: "idle", created: 1, at: 1, title: `Session ${id}`, waiting: 0,
  ...over,
});

beforeEach(() => {
  live.meta = {
    schema: 1, owner_ok: true, face_origin: "", general: "base",
    tracks: [{ name: "code", lane: "private", kind: "code" }, { name: "notes", lane: "private", kind: "private" }, { name: "base", lane: "private", kind: "private" }],
    spheres: [{ name: "travel", title: "Travel", icon: "✈️", track: "base" }],
  };
  live.dispatch({
    type: "list",
    snapshot: {
      schema: 1, cursor: 1,
      sessions: [session("w1", { state: "waiting", waiting: 1 }), session("r1", { state: "active" }), session("r2", { state: "active" })],
    },
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  live.stop();
  live.meta = null;
  feedSource.feed = null;
  sessionStorage.clear();
});

describe("/new, the empty new task", () => {
  it("greets for the time of day and says how things stand, a tap away from «Today»; no «To», no «Continue»", async () => {
    feedSource.feed = {
      schema: 1, count: 2, groups: [], bot: "", assistant_at: null,
      deadlines: [
        { id: "d2", when: "2026-11-03", title: "in 6 days: lease agreement", sphere: "travel", sphere_title: "Travel", sphere_icon: "✈️", path: null },
        { id: "d1", when: "2026-10-30", title: "in 2 days: Gym membership — valid until", sphere: null, sphere_title: null, sphere_icon: null, path: null },
      ],
    };
    const { router } = await mountNew({ now: new Date(2026, 9, 28, 19, 30) });
    expect(screen.getByRole("heading", { name: "Good evening" })).toBeTruthy();
    expect(screen.getByTestId("next-deadline").textContent).toBe("in 2 days: Gym membership — valid until");
    for (const gone of ["To", "Continue:", "New session", "from the Base…"]) expect(screen.queryByText(gone)).toBeNull();
    fireEvent.click(screen.getByRole("link", { name: "Today: 2 waiting · 2 working" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/today"));
  });

  it("sends into a new general session in the root of the Base — no sphere — and opens it", async () => {
    const newSession = vi.fn().mockResolvedValue({ session: "n1" });
    const { router } = await mountNew({ newSession });
    const field = screen.getByRole("textbox", { name: "What should I do?" });
    expect((screen.getByRole("button", { name: "Send" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(field, { target: { value: "find flights AMS→LIS on 2 June" } });
    fireEvent.change(screen.getByTestId("attach-input"), { target: { files: [new File(["abc"], "plan.txt", { type: "text/plain" })] } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/s/n1"));
    const [track, text, id, sphere, files] = newSession.mock.calls[0]!;
    expect([track, text, String(id).startsWith("face:"), sphere]).toEqual(["base", "find flights AMS→LIS on 2 June", true, null]);
    expect((files as { name: string }[]).map((f) => f.name)).toEqual(["plan.txt"]);
    expect(sessionStorage.getItem("retinue-new-draft")).toBeNull();
  });

  it("refuses with a line when the platform has no track in the root of the Base, and sends nothing before it answers", async () => {
    const newSession = vi.fn();
    live.meta = { ...live.meta!, general: null };
    await mountNew({ newSession });
    fireEvent.change(screen.getByRole("textbox", { name: "What should I do?" }), { target: { value: "a task" } });
    expect(screen.getByRole("alert").textContent).toBe(NO_TRACK);
    const arrow = screen.getByRole("button", { name: "Send" }) as HTMLButtonElement;
    expect([arrow.disabled, arrow.title]).toEqual([true, NO_TRACK]);
    cleanup();
    live.meta = null; // not answered yet
    await mountNew({ newSession });
    fireEvent.change(screen.getByRole("textbox", { name: "What should I do?" }), { target: { value: "a task" } });
    expect((screen.getByRole("button", { name: "Send" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(newSession).not.toHaveBeenCalled();
  });

  it("opens with a task begun elsewhere — its text, its sphere — and sends it there, the cursor after its text", async () => {
    const newSession = vi.fn().mockResolvedValue({ session: "n2" });
    startNew("About a letter from Acme:\n> pay: the invoice\n\n", "travel");
    const { router } = await mountNew({ newSession });
    const field = screen.getByRole("textbox", { name: "What should I do?" }) as HTMLTextAreaElement;
    expect(field.value).toBe("About a letter from Acme:\n> pay: the invoice\n\n");
    expect(field.selectionStart).toBe(field.value.length);
    expect(screen.getByRole("button", { name: "✈️ Travel" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.change(field, { target: { value: `${field.value}why is it due?` } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/s/n2"));
    expect(newSession.mock.calls[0]?.[3]).toBe("travel");
    expect([sessionStorage.getItem("retinue-new-draft"), sessionStorage.getItem("retinue-new-sphere")]).toEqual([null, null]);
    cleanup();
    startNew("x", "no-such-sphere");
    await mountNew();
    expect(screen.getByRole("button", { name: "Auto" }).getAttribute("aria-pressed")).toBe("true");
  });

  it("keeps what was typed and not sent for the next visit", async () => {
    await mountNew();
    fireEvent.change(screen.getByRole("textbox", { name: "What should I do?" }), { target: { value: "a draft" } });
    cleanup();
    await mountNew();
    expect((screen.getByRole("textbox", { name: "What should I do?" }) as HTMLTextAreaElement).value).toBe("a draft");
  });

  it("is «New task» on a wide screen: the sidebar's first line, the page in the middle", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("min-width"),
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }));
    vi.spyOn(live, "start").mockResolvedValue(undefined);
    vi.spyOn(feedSource, "start").mockImplementation(() => undefined);
    const router = makeRouter(createMemoryHistory({ initialEntries: ["/today"] }));
    render(<RouterProvider router={router} />);
    await act(async () => {
      await router.load();
    });
    const nav = screen.getAllByRole("navigation", { name: "Places" })[0]!;
    const first = within(nav).getAllByRole("link")[0]!;
    expect(first.textContent).toBe("New task");
    fireEvent.click(first);
    await waitFor(() => expect(router.state.location.pathname).toBe("/new"));
    const middle = document.querySelector("main.overflow-y-auto.min-h-0") as HTMLElement;
    expect(within(middle).getByRole("textbox", { name: "What should I do?" })).toBeTruthy();
  });
});

describe("its pieces", () => {
  it("the greeting by the hour", () => {
    expect([4, 5, 11, 12, 17, 18, 22, 23, 0].map(greeting)).toEqual([
      "Good night", "Good morning", "Good morning", "Good afternoon", "Good afternoon", "Good evening", "Good evening", "Good night", "Good night",
    ]);
  });

  it("the status line", () => {
    expect(statusLine(2, 1)).toBe("2 waiting · 1 working");
    expect(statusLine(0, 3)).toBe("nothing waiting · 3 working");
    expect(statusLine(1, 0)).toBe("1 waiting");
    expect(statusLine(0, 0)).toBe("Quiet: nothing waiting");
  });

  it("the general track: the platform's word; an older platform — its first track that is not a repository's; else none", () => {
    const meta = live.meta!;
    expect(generalTrack(meta)).toBe("base");
    const { general: _, ...older } = meta; // a platform before `general`
    expect(generalTrack(older)).toBe("notes");
    expect(generalTrack({ ...older, tracks: [{ name: "code", lane: "private", kind: "code" }] })).toBeNull();
    expect(generalTrack({ ...meta, general: null })).toBeNull();
    expect(generalTrack(null)).toBeNull();
  });
});
