// The session page (variant B): «Chat» and «Results» apart, what waits on him in a dock
// above the input box, decided cards as one-line receipts, one icon per relation.
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { upOf } from "@/components/relation";
import { Transcript } from "@/components/transcript/transcript";
import { type Waiting, WaitingDock, wholeOnRow } from "@/components/waiting-dock";
import { api } from "@/lib/api";
import { live } from "@/lib/live";
import { defaultTab, parseStat, resultsOf } from "@/lib/results";
import type { Approval, BaseCard, Input, Item, OutFile, Proposal, Session } from "@/lib/types";

const session = (over: Partial<Session> = {}): Session => ({
  id: "s1", track: "notes", lane: "private", kind: "private", state: "idle", created: 1, at: 50, title: "Lisbon", waiting: 0, ...over,
});
const file = (id: string, at: number, mime = "text/markdown"): OutFile => ({ id, session: "s1", path: `${id}.md`, name: `${id}.md`, size: 10, mime, at });
const base: BaseCard = {
  id: "b1", session: "s1", state: "committed", subject: "travel: Lisbon", sha: "4f2a9c1", said: "Written.", undo: true, undone: null,
  refused: [], committed: [], created: 30, at: 30,
  files: [{ path: "travel/AGENTS.md", status: "M", add: 1, del: 0 }, { path: "travel/journal/lisbon.md", status: "A", add: 18, del: 0 }],
};
const report: Input = {
  session: "s1", input_id: "subrep:1", by: "report", at: 40, turn: 1, n: 0, attachments: [],
  text: 'Sub-session "A flat" (b1c2d3e4): Three places fit.\n\nSub-session "A route" (f1e2d3c4) — turn cut short: By train.',
};
const proposal = (over: Partial<Proposal> = {}): Proposal => ({
  id: "p1", session: "s1", tool_use_id: "toolu_p", title: "Compare train fares", created: 10, at: 10, state: "open", child: null,
  from_report: false, brief: "Compare the morning trains to Porto.", ...over,
});
const approval = (over: Partial<Approval> = {}): Approval => ({
  id: "a1", session: "s1", kind: "approval", tool_use_id: "toolu_b", state: "shown", tool: "Bash", input: { command: "git tag v1" },
  cwd: "/work", digest: "d", view: { head: ["Approval · notes · session s1"] }, created: 5, at: 5, ...over,
});

async function mount(node: React.ReactNode) {
  const root = createRootRoute({ component: () => <>{node}</> });
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => null });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => null });
  const router = createRouter({ routeTree: root.addChildren([home, one]), history: createMemoryHistory({ initialEntries: ["/"] }) });
  render(<RouterProvider router={router} />);
  await act(async () => {
    await router.load();
  });
  return router;
}

afterEach(() => {
  cleanup();
  live.dispatch({ type: "reset" });
  vi.restoreAllMocks();
});

describe("«Results»", () => {
  it("lists everything the session made, newest first, with what stands now — the diff, the push — on top", () => {
    const rows = resultsOf({
      session: session({ push: 2 }),
      files: [file("old", 10), file("page", 60, "text/html; charset=utf-8")],
      bases: { b1: base },
      summaries: [],
      inputs: [report],
      ending: { kind: "look", artifact: { path: "out/mockup.png", title: "mockup.png", src: "/api/x", kind: "image" } },
      endingAt: 45,
      pushes: {},
      diff: { files: 3, added: 27, removed: 3 },
    });
    expect(rows.map((r) => r.kind)).toEqual(["push", "diff", "page", "look", "report", "report", "base", "file"]);
    const reports = rows.filter((r) => r.kind === "report");
    expect(reports.map((r) => r.kind === "report" && [r.title, r.child, r.cut])).toEqual([["A flat", "b1c2d3e4", false], ["A route", "f1e2d3c4", true]]);
  });

  it("reads the diff's numbers from its stat, and nothing from no change", () => {
    expect(parseStat(" a | 6 ++++++\n 3 files changed, 27 insertions(+), 3 deletions(-)")).toEqual({ files: 3, added: 27, removed: 3 });
    expect(parseStat(" 1 file changed, 1 insertion(+)")).toEqual({ files: 1, added: 1, removed: 0 });
    expect(parseStat("")).toBeNull();
  });

  it("is where a finished session opens; one working or waiting opens on the conversation", () => {
    expect(defaultTab(session({ state: "idle" }), 3)).toBe("results");
    expect(defaultTab(session({ state: "closed" }), 1)).toBe("results");
    expect(defaultTab(session({ state: "idle" }), 0)).toBe("chat");
    expect(defaultTab(session({ state: "active" }), 3)).toBe("chat");
    expect(defaultTab(session({ state: "waiting" }), 3)).toBe("chat");
  });
});

describe("«Waiting for you»", () => {
  const rows = (...w: Waiting[]) => w;

  it("starts a short brief from its row, the brief read whole there", async () => {
    const press = vi.spyOn(api, "proposal").mockResolvedValue({ say: "Started.", proposal: proposal({ state: "started", child: "c1" }) });
    await mount(<WaitingDock waiting={rows({ kind: "proposal", key: "p:p1", at: 10, proposal: proposal() })} open={null} onOpen={() => undefined} />);
    const dock = screen.getByRole("region", { name: "Waiting for you" });
    expect(within(dock).getByText("Compare the morning trains to Porto.")).toBeTruthy();
    fireEvent.click(within(dock).getByRole("button", { name: "Start" }));
    await vi.waitFor(() => expect(press).toHaveBeenCalledWith("p1", "start"));
  });

  it("opens a long brief, or one a report's turn proposed, before it can be started: nothing starts unseen", async () => {
    const onOpen = vi.fn();
    const long = proposal({ brief: "line\n".repeat(20) });
    const told = proposal({ id: "p2", from_report: true });
    await mount(
      <WaitingDock
        waiting={rows({ kind: "proposal", key: "p:p1", at: 10, proposal: long }, { kind: "proposal", key: "p:p2", at: 11, proposal: told })}
        open={null}
        onOpen={onOpen}
      />,
    );
    expect(screen.queryByRole("button", { name: "Start" })).toBeNull();
    const shows = screen.getAllByRole("button", { name: "Show the brief" });
    expect(shows).toHaveLength(2);
    fireEvent.click(shows[0]!);
    expect(onOpen).toHaveBeenCalledWith("p:p1");
  });

  it("allows a one-line command from its row; a long or hiding one opens its whole card first", () => {
    expect(wholeOnRow(approval())).toBe(true);
    expect(wholeOnRow(approval({ input: { command: "a\nb" } }))).toBe(false);
    expect(wholeOnRow(approval({ input: { command: "x".repeat(200) } }))).toBe(false);
    expect(wholeOnRow(approval({ input: { command: "echo​hi" } }))).toBe(false);
    expect(wholeOnRow(approval({ input: { command: "ls", dangerouslyDisableSandbox: true } }))).toBe(false);
  });

  it("shows the whole card under its row once opened, and says how many wait", async () => {
    await mount(
      <WaitingDock
        waiting={rows({ kind: "approval", key: "a:a1", at: 5, approval: approval({ input: { command: "a\nb" } }) })}
        open="a:a1"
        onOpen={() => undefined}
      />,
    );
    const dock = screen.getByRole("region", { name: "Waiting for you" });
    expect(within(dock).getByRole("heading").textContent).toBe("Waiting for you1");
    expect(within(dock).getByTestId("approval-card")).toBeTruthy();
  });
});

describe("the conversation", () => {
  const call = (id: string, tool: string): Item =>
    ({ session: "s1", turn: 0, n: 0, kind: "tool", data: { id, tool, input: { title: "x" }, result: "Proposed to the owner.", error: false } }) as Item;

  it("hides a call's raw line whenever a card stands for it; a waiting one is an anchor, a decided one its receipt", async () => {
    const onWaiting = vi.fn();
    await mount(
      <Transcript
        items={[call("toolu_p", "mcp__retinue__propose_subsession")]}
        approvals={[]}
        proposals={[proposal()]}
        running={false}
        onWaiting={onWaiting}
      />,
    );
    expect(screen.queryByText(/propose_subsession/)).toBeNull();
    fireEvent.click(screen.getByTestId("waiting-anchor"));
    expect(onWaiting).toHaveBeenCalledWith("p:p1");
    cleanup();
    await mount(<Transcript items={[call("toolu_b", "Bash")]} approvals={[approval({ state: "executed" })]} running={false} />);
    const receipt = screen.getByTestId("receipt");
    expect(receipt.querySelector("summary")!.textContent).toContain("Allowed git tag v1");
    expect((receipt as HTMLDetailsElement).open).toBe(false); // the card is inside, folded
  });

  it("says a sub-sessions' report in one line, the reports under it with their provenance", async () => {
    await mount(<Transcript items={[]} approvals={[]} inputs={[report]} running={false} />);
    const line = screen.getByTestId("reports-receipt");
    expect(line.querySelector("summary")!.textContent).toBe("2 sub-sessions reported: A flat, A route");
    expect(within(line).getByTestId("reports")).toBeTruthy();
  });
});

describe("a relation", () => {
  it("is one of three, the same wherever it is drawn", () => {
    expect(upOf(session({ sub_of: "p" }))).toEqual({ id: "p", how: "sub-session" });
    expect(upOf(session({ parent: "p", side: true }))).toEqual({ id: "p", how: "side question" });
    expect(upOf(session({ fork_of: "p" }))).toEqual({ id: "p", how: "fork" });
    expect(upOf(session())).toBeNull();
  });
});
