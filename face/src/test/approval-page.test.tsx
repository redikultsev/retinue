import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import { live } from "@/lib/live";
import type { Approval } from "@/lib/types";
import { ApprovalPage } from "@/pages/approval";

const card: Approval = {
  id: "5f3a9c21",
  session: "a1b2c3d4",
  tool_use_id: "toolu_1",
  kind: "approval",
  state: "shown",
  tool: "Bash",
  input: { command: "make test" },
  cwd: "/work/retinue",
  digest: "d",
  view: { head: ["Approval · retinue · session a1b2c3d4"] },
  created: 1,
  at: 1,
};

function mount(load: (id: string) => Promise<{ approval: Approval }>, extra: Partial<Parameters<typeof ApprovalPage>[0]> = {}) {
  const root = createRootRoute();
  const page = createRoute({
    getParentRoute: () => root,
    path: "/a/$id",
    component: () => <ApprovalPage id="5f3a9c21" load={load} {...extra} />,
  });
  const session = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>Session opened</p> });
  const index = createRoute({ getParentRoute: () => root, path: "/", component: () => null });
  const router = createRouter({
    routeTree: root.addChildren([page, session, index]),
    history: createMemoryHistory({ initialEntries: ["/a/5f3a9c21"] }),
  });
  return render(<RouterProvider router={router} />);
}

describe("ApprovalPage", () => {
  it("loads the card by id and watches its session", async () => {
    const watch = vi.spyOn(live, "watch").mockResolvedValue();
    const load = vi.fn().mockResolvedValue({ approval: card });
    mount(load);
    expect(await screen.findByText("Approval · retinue · session a1b2c3d4")).toBeTruthy();
    expect(load).toHaveBeenCalledWith("5f3a9c21");
    expect(watch).toHaveBeenCalledWith("a1b2c3d4");
    expect(screen.getByRole("button", { name: "Allow" })).toBeTruthy();
    watch.mockRestore();
    live.stop();
  });

  it("says so when the card does not exist", async () => {
    mount(vi.fn().mockRejectedValue(new ApiError(404, "x")));
    expect(await screen.findByText("There is no such card.")).toBeTruthy();
  });

  it("in Telegram closes the Mini App a second after a decision, the result shown till then", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const watch = vi.spyOn(live, "watch").mockResolvedValue();
    const close = vi.fn().mockReturnValue(true);
    const decided = { ...card, state: "granted" as const, view: { ...card.view, status: "Allowed — running it." } };
    const decide = vi.fn().mockResolvedValue({ toast: "Allowed.", approval: decided });
    mount(vi.fn().mockResolvedValue({ approval: card }), { decide, telegram: () => true, close });
    fireEvent.click(await screen.findByRole("button", { name: "Allow" }));
    expect(await screen.findByText("Allowed — running it. Closing…")).toBeTruthy();
    expect(close).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTime(1000));
    expect(close).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
    watch.mockRestore();
    live.stop();
  });

  it("elsewhere goes on to the session; a stale press stays", async () => {
    const watch = vi.spyOn(live, "watch").mockResolvedValue();
    const close = vi.fn();
    const stale = vi.fn().mockResolvedValue({ toast: "The card is stale — the decision was not taken.", approval: { ...card, state: "stale" } });
    const first = mount(vi.fn().mockResolvedValue({ approval: card }), { decide: stale, telegram: () => false, close });
    fireEvent.click(await screen.findByRole("button", { name: "Allow" }));
    await vi.waitFor(() => expect(stale).toHaveBeenCalled());
    expect(screen.queryByText("Session opened")).toBeNull();
    first.unmount();
    live.stop();
    const decide = vi.fn().mockResolvedValue({ toast: "Denied.", approval: { ...card, state: "rejected" } });
    mount(vi.fn().mockResolvedValue({ approval: card }), { decide, telegram: () => false, close });
    fireEvent.click(await screen.findByRole("button", { name: "Deny" }));
    expect(await screen.findByText("Session opened")).toBeTruthy();
    expect(close).not.toHaveBeenCalled();
    watch.mockRestore();
    live.stop();
  });
});
