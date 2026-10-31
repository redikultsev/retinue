import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SessionSummary } from "@/components/session-summary";
import { live } from "@/lib/live";
import { childrenOf, latestPlan, planLine, tokensWord } from "@/lib/summary";
import type { Item, Session, SessionSnapshot } from "@/lib/types";

const session = (id: string, over: Partial<Session> = {}): Session => ({
  id, track: "retinue", lane: "code", kind: "code", state: "idle", created: 1, at: 1, title: `Session ${id}`, waiting: 0, ...over,
});
const plan = (n: number, statuses: string[]): Item =>
  ({
    session: "s1", turn: 0, n, kind: "plan",
    data: { id: `p${n}`, tool: "TodoWrite", todos: statuses.map((status, k) => ({ content: `step ${k}`, status, activeForm: `doing ${k}` })) },
  }) as Item;

afterEach(() => live.stop());

function mount(node: React.ReactNode) {
  const root = createRootRoute();
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const router = createRouter({ routeTree: root.addChildren([home]), history: createMemoryHistory({ initialEntries: ["/"] }) });
  return render(<RouterProvider router={router} />);
}

describe("«Summary»", () => {
  it("reads the latest plan, the step in work, children of every kind and tokens in words", () => {
    const items = [plan(0, ["pending"]), plan(1, ["completed", "in_progress", "pending"])];
    expect(latestPlan(items)?.map((t) => t.status)).toEqual(["completed", "in_progress", "pending"]);
    expect(planLine(latestPlan(items)!)).toEqual({ done: 1, total: 3, now: "doing 1" });
    expect(latestPlan([])).toBeNull();
    const all = {
      p: session("p"), a: session("a", { sub_of: "p", at: 3 }), b: session("b", { parent: "p", at: 2 }),
      c: session("c", { fork_of: "p", at: 1 }), x: session("x"),
    };
    expect(childrenOf(all, "p").map((k) => [k.session.id, k.how])).toEqual([["a", "sub-session"], ["b", "side question"], ["c", "fork"]]);
    expect([tokensWord(840), tokensWord(84_200), tokensWord(1_240_000)]).toEqual(["840", "84k", "1.2M"]);
  });

  it("shows only what the platform said: environment, context, plan, sub-sessions, connectors", async () => {
    const snapshot: SessionSnapshot = {
      schema: 1, cursor: 5, session: session("s1", { state: "active" }), approvals: [], summaries: [],
      items: [plan(0, ["completed", "in_progress"])],
      about: { cwd: "/work/retinue", context_tokens: 84_000, model: "claude-opus-5-5", cli_version: "2.1.304",
        permission_mode: null, mcp: [{ name: "retinue", status: "connected" }] },
    };
    live.dispatch({ type: "list", snapshot: { schema: 1, cursor: 4, sessions: [session("s1"), session("k1", { sub_of: "s1", title: "lodging" })] } });
    live.dispatch({ type: "session", snapshot });
    mount(<SessionSummary id="s1" />);
    const env = await screen.findByRole("region", { name: "Environment" });
    expect(within(env).getByText("/work/retinue")).toBeTruthy();
    expect(within(env).getByText("claude-opus-5-5")).toBeTruthy();
    expect(within(env).queryByText("Mode")).toBeNull(); // not said: not shown
    expect(screen.getByRole("region", { name: "Context" }).textContent).toContain("84k");
    expect(screen.getByRole("region", { name: "Plan" }).textContent).toContain("1 of 2");
    expect(within(screen.getByRole("region", { name: "Sub-sessions" })).getByText("lodging")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "Connectors" })).getByText("retinue")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Files" })).toBeNull();
  });
});
