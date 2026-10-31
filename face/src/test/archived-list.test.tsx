import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ArchivedList } from "@/components/archived-list";
import { SessionList } from "@/components/session-list";
import { api } from "@/lib/api";
import { live } from "@/lib/live";
import type { ArchivedSession, Session } from "@/lib/types";

const NOW = new Date(2026, 9, 28, 12, 0).getTime() / 1000;
const row = (id: string, over: Partial<ArchivedSession> = {}): ArchivedSession => ({
  id, track: "notes", lane: "private", kind: "private", state: "closed", created: 1, at: 1, title: `Session ${id}`, waiting: 0,
  archived: true, archived_at: NOW - 3600, ...over,
});

async function mount(node: React.ReactNode) {
  const root = createRootRoute({ component: () => <>{node}</> });
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => null });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>session opened</p> });
  const router = createRouter({ routeTree: root.addChildren([home, one]), history: createMemoryHistory({ initialEntries: ["/"] }) });
  render(<RouterProvider router={router} />);
  await act(async () => {
    await router.load();
  });
  return router;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  live.dispatch({ type: "reset" });
  live.meta = null;
});

describe("«Archived»", () => {
  it("lists what was archived, the latest first, and «Restore» puts one back in every list", async () => {
    live.meta = { schema: 1, owner_ok: true, face_origin: "", tracks: [], spheres: [{ name: "travel", title: "Travel", icon: "✈️", track: "notes" }] };
    const load = vi.fn().mockResolvedValue({ sessions: [row("a1", { sphere: "travel", title: "Trip plans" }), row("b2")] });
    const back: Session = { ...row("a1", { sphere: "travel", title: "Trip plans" }), archived: undefined };
    const restore = vi.fn().mockResolvedValue({ session: back });
    const router = await mount(<ArchivedList load={load} restore={restore} now={NOW} />);
    const rows = await screen.findAllByTestId("archived-row");
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining("Trip plans✈️ Travel · archived"),
      expect.stringContaining("Session b2archived"),
    ]);
    fireEvent.click(screen.getAllByRole("button", { name: "Restore" })[0]!);
    await waitFor(() => expect(restore).toHaveBeenCalledWith("a1", false));
    await waitFor(() => expect(screen.getAllByTestId("archived-row")).toHaveLength(1));
    expect(live.getState().sessions.a1?.archived).toBeUndefined();
    fireEvent.click(screen.getByRole("link", { name: "Session b2" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/s/b2"));
  });

  it("says so when nothing is archived", async () => {
    await mount(<ArchivedList load={vi.fn().mockResolvedValue({ sessions: [] })} />);
    expect(await screen.findByText("Nothing archived.")).toBeTruthy();
  });

  it("is the last of the list's filters, and another filter leaves it", async () => {
    vi.spyOn(api, "archived").mockResolvedValue({ sessions: [row("a1")] });
    await mount(<SessionList />);
    fireEvent.click(screen.getByRole("button", { name: "Archived" }));
    expect(await screen.findByTestId("archived-row")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Archived" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "All" }));
    expect(screen.queryByTestId("archived-row")).toBeNull();
    expect(screen.getByRole("button", { name: "All" }).getAttribute("aria-pressed")).toBe("true");
  });
});
