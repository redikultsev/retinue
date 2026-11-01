// What the agents proposed to write to the collections (retinue/colltools.py): the card says what each operation would
// change, the writer's words and why it is the owner's; «Apply» writes it, «Dismiss» sets it aside; a record that moved
// on since says who moved it and leaves only «Dismiss». A Space shows its own first and reads them again when the
// stream tells of one; «Waiting for you» reads itself again too.
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CollectionProposalCard, proposalTitle } from "@/components/collection-proposal-card";
import { SpaceProposals } from "@/components/views/proposals";
import { ApiError } from "@/lib/api";
import { editApi, type ProposalsApi } from "@/lib/edit";
import { toast } from "@/lib/toast";
import { DEBOUNCE_MS, type Feed, FeedSource } from "@/lib/feed";
import type { Live } from "@/lib/live";
import { initialState } from "@/lib/store";
import type { CollectionProposal } from "@/lib/view-types";

vi.mock("@/lib/toast", async (original) => ({ ...(await original<typeof import("@/lib/toast")>()), toast: vi.fn() }));

const DEAL = "01J8ZQ4Y7Z5G00000000000001";
const proposal = (over: Partial<CollectionProposal> = {}): CollectionProposal => ({
  id: "0123456789ab", tool_use_id: "toolu_p", session: "s1", actor: "session:s1", collection: "deals", space: "work",
  changes: [
    { op: "set", id: DEAL, before: { stage: "qualified" }, after: { stage: "proposal" }, title: "Initech" },
    { op: "delete", id: DEAL, before: { _deleted: null }, after: { _deleted: 100 }, title: "Hooli" },
  ],
  fields: { stage: { type: "enum", values: ["lead", "qualified", "proposal"] } }, words: {},
  why: "Their reply asks for a quote by Friday.", needs: "a delete by session in deals is the owner's to make",
  created: 10, at: 10, state: "open", change_ids: null, ...over,
});

async function mount(node: React.ReactNode) {
  const root = createRootRoute({ component: () => <>{node}</> });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>session opened</p> });
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => null });
  const router = createRouter({ routeTree: root.addChildren([home, one]), history: createMemoryHistory({ initialEntries: ["/"] }) });
  render(<RouterProvider router={router} />);
  await act(async () => {
    await router.load();
  });
}

const fakeApi = (over: Partial<ProposalsApi> = {}) => ({ waiting: vi.fn(), decide: vi.fn(), ...over }) as unknown as ProposalsApi & { [K in keyof ProposalsApi]: ReturnType<typeof vi.fn> };

afterEach(cleanup);

describe("a proposed write", () => {
  it("says what each operation would change, the session's words and why it is the owner's; «Apply» writes it", async () => {
    const applied = proposal({ state: "applied", at: 20, change_ids: ["01J8ZQ4Y7Z5G0000000000CHG1", "01J8ZQ4Y7Z5G0000000000CHG2"] });
    const api = fakeApi({ decide: vi.fn().mockResolvedValue({ proposal: applied }) });
    const onDecided = vi.fn();
    await mount(<CollectionProposalCard proposal={proposal()} api={api} onDecided={onDecided} />);
    expect(proposalTitle(proposal())).toBe("Deals · 2 changes");
    const card = screen.getByTestId("collection-proposal");
    expect(card.textContent).toContain("Initech");
    expect(card.textContent).toContain("Stage: Qualified → Proposal");
    expect(card.textContent).toContain("Hooli");
    expect(card.textContent).toContain("deleted");
    expect(screen.getByTestId("proposal-why").textContent).toBe("Their reply asks for a quote by Friday.");
    expect(card.textContent).toContain("Yours to apply: a delete by session in deals is the owner's to make");
    expect(screen.getByRole("link", { name: "A session" }).getAttribute("href")).toBe("/s/s1");
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(onDecided).toHaveBeenCalledWith(applied));
    expect(api.decide).toHaveBeenCalledWith("0123456789ab", "apply");
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
    expect(card.textContent).toContain("applied");
  });

  it("«Applied.» has «Undo», which takes back what it wrote", async () => {
    const CHG = "01J8ZQ4Y7Z5G0000000000CHG1";
    const written = { name: "deals", rev: "2", change_ids: [CHG], records: [{ id: DEAL, fields: { stage: "proposal" }, version: 3, created: 0, updated: 0, deleted: null }] };
    const api = fakeApi({ decide: vi.fn().mockResolvedValue({ proposal: proposal({ state: "applied", at: 20 }), answer: written }) });
    const write = vi.spyOn(editApi, "write").mockResolvedValue({ ...written, change_ids: ["01J8ZQ4Y7Z5G0000000000CHG2"] });
    await mount(<CollectionProposalCard proposal={proposal()} api={api} />);
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Applied.", "info", expect.any(Number), undefined, expect.objectContaining({ label: "Undo" })));
    const action = vi.mocked(toast).mock.calls.at(-1)![4]!;
    action.run();
    await waitFor(() => expect(write).toHaveBeenCalledWith("deals", [{ op: "revert", change_id: CHG, version: 3 }], expect.any(String)));
    write.mockRestore();
  });

  it("names who moved a record on since, and then only «Dismiss» is left", async () => {
    const refusal = {
      error: "ops[0]: stale", kind: "stale", record: null,
      change: { change_id: "01J8ZQ4Y7Z5G0000000000CHG9", ts: Date.now() / 1000 - 120, op: "set", before: { stage: "qualified" }, after: { stage: "lost" }, actor: "assistant" },
      proposal: proposal(),
    };
    const dismissed = proposal({ state: "dismissed", at: 30 });
    const api = fakeApi({ decide: vi.fn().mockRejectedValueOnce(new ApiError(409, "stale", refusal)).mockResolvedValueOnce({ proposal: dismissed }) });
    await mount(<CollectionProposalCard proposal={proposal()} api={api} />);
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/^Changed by the assistant 2 min ago: Stage: Qualified → Lost\. Dismiss it/);
    expect((screen.getByRole("button", { name: "Apply" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull());
    expect(api.decide).toHaveBeenLastCalledWith("0123456789ab", "dismiss");
  });

  it("shows five operations, and the rest on asking", async () => {
    const many = proposal({ changes: Array.from({ length: 7 }, (_, n) => ({ op: "add" as const, id: DEAL, before: null, after: { company: `Acme ${n}` }, title: `Acme ${n}` })) });
    await mount(<CollectionProposalCard proposal={many} api={fakeApi()} />);
    expect(screen.queryByText("Acme 6")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show all 7" }));
    expect(screen.getByText("Acme 6")).toBeTruthy();
  });
});

describe("a Space's proposed writes", () => {
  function fakeSource() {
    const told = new Set<(p: CollectionProposal) => void>();
    const resynced = new Set<() => void>();
    return {
      source: {
        onCollectionProposal: (l: (p: CollectionProposal) => void) => {
          told.add(l);
          return () => told.delete(l);
        },
        onResync: (l: () => void) => {
          resynced.add(l);
          return () => resynced.delete(l);
        },
      } as unknown as Live,
      tell: (p: CollectionProposal) => {
        for (const l of told) l(p);
      },
    };
  }

  it("shows nothing while none waits, the Space's own when they come, and reads them again for its own alone", async () => {
    const api = fakeApi({ waiting: vi.fn().mockResolvedValueOnce({ proposals: [] }).mockResolvedValue({ proposals: [proposal()] }) });
    const { source, tell } = fakeSource();
    await mount(<SpaceProposals space="work" api={api} source={source} />);
    await waitFor(() => expect(api.waiting).toHaveBeenCalledWith("work"));
    expect(screen.queryByLabelText("Proposed")).toBeNull();
    tell(proposal({ space: "home" }));
    expect(api.waiting).toHaveBeenCalledTimes(1);
    tell(proposal());
    expect(await screen.findByTestId("collection-proposal")).toBeTruthy();
    expect(screen.getByLabelText("Proposed").textContent).toContain("Proposed (1)");
  });
});

describe("«Waiting for you»", () => {
  it("reads itself again when the stream tells of a proposed write", async () => {
    vi.useFakeTimers();
    try {
      const feed = { schema: 1, count: 0, groups: [], bot: "", assistant_at: null } as Feed;
      const load = vi.fn().mockResolvedValue(feed);
      const source = new FeedSource({ load, setTimer: setTimeout, clearTimer: clearTimeout });
      let told: () => void = () => {};
      const onCollectionProposal = (l: () => void) => {
        told = l;
        return () => {};
      };
      source.start({ getState: () => initialState, subscribe: () => () => {}, onCollectionProposal });
      await vi.waitFor(() => expect(load).toHaveBeenCalledTimes(1));
      told();
      await vi.advanceTimersByTimeAsync(DEBOUNCE_MS + 10);
      expect(load).toHaveBeenCalledTimes(2);
      source.stop();
    } finally {
      vi.useRealTimers();
    }
  });
});
