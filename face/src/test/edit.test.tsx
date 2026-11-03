// The owner's writes from a Space's screen: a queue's row pressed «Done» leaves at once and its toast takes it back; a
// card dropped on another column is moved at the version shown, and one that moved on since asks «Apply mine» or
// «Keep»; a record's sheet moves it and takes each change back; an add asks only for the view's fields, waits for the
// required ones, and asks «Add anyway» of one that may be there already.
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Toaster } from "@/components/toaster";
import { ApiError } from "@/lib/api";
import { type CollectionViews, forgetViews, type RecordAnswer, type Refusal, type SpaceViews, type View, type ViewRow, type WriteAnswer } from "@/lib/collections";
import { actorWords, addFields, againAt, changeWords, conflictWords, type EditApi, startValues, undoSteps, valueText } from "@/lib/edit";
import { live } from "@/lib/live";
import type { SpaceView } from "@/lib/spaces";
import { forgetRead, SpacePage } from "@/pages/spaces";

const REV = "01J8ZQ4Y7Z5G0000000000000A";
const ids = { initech: "01J8ZQ4Y7Z5G00000000000001", globex: "01J8ZQ4Y7Z5G00000000000002", acme: "01J8ZQ4Y7Z5G00000000000003", f1: "01J8ZQ4Y7Z5G00000000000011", f2: "01J8ZQ4Y7Z5G00000000000012" };
const row = (id: string, fields: Record<string, unknown>, version = 1, refs: Record<string, string> = {}): ViewRow => ({ id, version, fields, refs, src: null, days: 1, rotten: false });

const QUEUE: View = {
  name: "waiting", kind: "queue", title: "Waiting on me", rot: 2, fields: ["deal", "channel"], count: 2, dropped: 0,
  rows: [row(ids.f1, { deal: ids.initech, channel: "mail" }, 3, { deal: "Initech" }), row(ids.f2, { deal: ids.globex, channel: "phone" }, 1, { deal: "Globex" })],
  done: { field: "state", value: "done", label: "Done" },
  add: { label: "Add a follow-up", fields: ["deal", "date", "state"], values: { state: "draft" } },
};
const BOARD: View = {
  name: "pipeline", kind: "board", title: "Pipeline", group: "stage", rot: 7, fields: ["company"], dropped: 0,
  columns: [
    { value: "lead", count: 2, folded: false, rows: [row(ids.initech, { company: "Initech" }, 2), row(ids.globex, { company: "Globex" })] },
    { value: "proposal", count: 1, folded: false, rows: [row(ids.acme, { company: "Acme" })] },
    { value: "lost", count: 0, folded: true, rows: [] },
  ],
};
const WORK: SpaceViews = {
  space: "work", tz: "Europe/Berlin", today: "2026-09-16", at: 0,
  collections: [{ name: "deals", rev: REV, views: [{ ...BOARD, tab: "Board" } as View] }, { name: "followups", rev: REV, views: [QUEUE] }] satisfies CollectionViews[],
};
const SHOWCASE: SpaceView = {
  name: "work", title: "Work", rules: "", marker: true, counts: { questions: 0, tasks: 0 }, sections: [],
  records: { knowledge: [], profile: [], journal: [], artifacts: [] },
};

const answered = (collection: string, id: string, version: number, fields: Record<string, unknown> = {}): WriteAnswer => ({
  name: collection, rev: "01J8ZQ4Y7Z5G0000000000000B", change_ids: ["01J8ZQ4Y7Z5G0000000000CHG1"],
  records: [{ id, fields, version, created: 0, updated: 0, deleted: null }],
});

function mount(edit: Partial<EditApi>) {
  const views = { space: vi.fn().mockResolvedValue(WORK), view: vi.fn().mockRejectedValue(new Error("not asked")) };
  const api = { write: vi.fn(), record: vi.fn(), form: vi.fn(), changes: vi.fn(), ...edit } as unknown as EditApi;
  const root = createRootRoute();
  const page = createRoute({
    getParentRoute: () => root, path: "/",
    component: () => (
      <>
        <SpacePage space="work" read={vi.fn().mockResolvedValue({ head: "h", space: SHOWCASE })} views={views} edit={api} />
        <Toaster />
      </>
    ),
  });
  const router = createRouter({ routeTree: root.addChildren([page]), history: createMemoryHistory({ initialEntries: ["/"] }) });
  render(<RouterProvider router={router} />);
  return api as unknown as { [K in keyof EditApi]: ReturnType<typeof vi.fn> };
}

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

const refused = (refusal: Partial<Refusal>, status = 409) =>
  new ApiError(status, refusal.error ?? "refused", { error: "refused", kind: null, record: null, change: null, ...refusal });

describe("writing from the screen", () => {
  it("presses «Done» on a row at its version: the row leaves at once, and its toast takes it back", async () => {
    const api = mount({ write: vi.fn().mockResolvedValueOnce(answered("followups", ids.f1, 4)).mockResolvedValueOnce(answered("followups", ids.f1, 5)) });
    const queue = await screen.findByTestId("queue-waiting");
    fireEvent.click(within(queue).getByRole("button", { name: "Done: Initech" }));
    expect(within(queue).queryByText("Initech")).toBeNull();
    await waitFor(() => expect(api.write).toHaveBeenCalledTimes(1));
    expect(api.write.mock.calls[0]!.slice(0, 2)).toEqual(["followups", [{ op: "set", id: ids.f1, version: 3, fields: { state: "done" } }]]);
    expect(api.write.mock.calls[0]![2]).toMatch(/^face:/);
    fireEvent.click(await screen.findByRole("button", { name: "Undo" }));
    await waitFor(() => expect(api.write).toHaveBeenCalledTimes(2));
    expect(api.write.mock.calls[1]![1]).toEqual([{ op: "revert", change_id: "01J8ZQ4Y7Z5G0000000000CHG1", version: 4 }]);
  });

  it("marks all the rows done after asking once, and puts back a row whose write failed", async () => {
    const api = mount({ write: vi.fn().mockRejectedValue(new ApiError(503, "The store is busy")) });
    const queue = await screen.findByTestId("queue-waiting");
    fireEvent.click(screen.getByRole("button", { name: "Mark all 2: Done" }));
    const asked = screen.getByRole("dialog", { name: "Done: all 2?" });
    expect(within(asked).getAllByRole("listitem").map((l) => l.textContent)).toEqual(["Initech · mail", "Globex · phone"]);
    fireEvent.click(within(asked).getByRole("button", { name: "Mark 2" }));
    await waitFor(() => expect(api.write).toHaveBeenCalledTimes(1));
    expect(api.write.mock.calls[0]![1]).toHaveLength(2);
    expect(await screen.findByText("The store is busy")).toBeTruthy();
    await waitFor(() => expect(within(queue).getByText("Initech")).toBeTruthy());
  });

  it("moves a card dropped on another column; one moved on since asks, and «Apply mine» writes at its new version", async () => {
    const moved: Refusal = {
      error: "stale", kind: "stale", record: { id: ids.initech, fields: { company: "Initech", stage: "lost" }, version: 3, created: 0, updated: 0, deleted: null },
      change: { change_id: REV, ts: Date.now() / 1000 - 120, as_of: null, batch: null, collection: "deals", id: ids.initech, op: "set", before: { stage: "lead" }, after: { stage: "lost" }, actor: "assistant", model: null, via: "assistant", src: null, foreign: ["mail"], reverts: null, idem: null },
    };
    const api = mount({ write: vi.fn().mockRejectedValueOnce(refused(moved)).mockResolvedValueOnce(answered("deals", ids.initech, 4)) });
    fireEvent.click(await screen.findByRole("tab", { name: "Board" }));
    const wide = await screen.findByTestId("board-wide-pipeline");
    const card = within(wide).getByText("Initech").closest("li")!;
    const data = new Map<string, string>();
    const transfer = { setData: (k: string, v: string) => data.set(k, v), getData: (k: string) => data.get(k) ?? "", get types() { return [...data.keys()]; }, effectAllowed: "", dropEffect: "" };
    fireEvent.dragStart(card, { dataTransfer: transfer });
    const proposal = within(wide).getByTestId("column-proposal");
    fireEvent.dragOver(proposal, { dataTransfer: transfer });
    fireEvent.drop(proposal, { dataTransfer: transfer });
    expect(within(proposal).getByText("Initech")).toBeTruthy(); // at once, before the answer
    const asked = await screen.findByTestId("conflict");
    expect(asked.textContent).toContain("Changed by the assistant 2 min ago: Stage: Lead → Lost.");
    expect(api.write.mock.calls[0]![1]).toEqual([{ op: "set", id: ids.initech, version: 2, fields: { stage: "proposal" } }]);
    fireEvent.click(within(asked).getByRole("button", { name: "Apply mine" }));
    await waitFor(() => expect(api.write).toHaveBeenCalledTimes(2));
    expect(api.write.mock.calls[1]![1]).toEqual([{ op: "set", id: ids.initech, version: 3, fields: { stage: "proposal" } }]);
  });

  it("opens a card's sheet: «Move to» another column, and «Undo» on a change of its history", async () => {
    const record: RecordAnswer = {
      name: "deals", rev: REV, title: "Acme", fields: { company: { type: "text", required: true }, stage: { type: "enum", values: ["lead", "proposal", "lost"] }, n: { type: "int", hide: true } },
      record: { id: ids.acme, fields: { company: "Acme", stage: "proposal", n: 7 }, version: 2, created: 0, updated: 0, deleted: null },
      changes: [
        { change_id: "01J8ZQ4Y7Z5G0000000000CHG2", ts: Date.now() / 1000 - 3600, as_of: null, batch: null, collection: "deals", id: ids.acme, op: "set", before: { stage: "lead" }, after: { stage: "proposal" }, actor: "assistant", model: null, via: "assistant", src: null, foreign: ["mail"], reverts: null, idem: null },
        { change_id: "01J8ZQ4Y7Z5G0000000000CHG3", ts: Date.now() / 1000 - 86400 * 3, as_of: null, batch: null, collection: "deals", id: ids.acme, op: "add", before: null, after: { company: "Acme", stage: "lead" }, actor: "owner", model: null, via: "screen", src: null, foreign: [], reverts: null, idem: null },
      ],
      words: {},
    };
    const api = mount({ record: vi.fn().mockResolvedValue(record), write: vi.fn().mockResolvedValue(answered("deals", ids.acme, 3)) });
    fireEvent.click(await screen.findByRole("tab", { name: "Board" }));
    const wide = await screen.findByTestId("board-wide-pipeline");
    fireEvent.click(within(wide).getByRole("button", { name: /Acme/ }));
    const sheet = await screen.findByTestId("record-sheet");
    expect(api.record).toHaveBeenCalledWith("deals", ids.acme);
    await within(sheet).findByText("Stage: Lead → Proposal");
    expect(within(sheet).getByText("the assistant · 1 h ago · after reading mail")).toBeTruthy();
    expect(within(sheet).getByRole("button", { name: "Proposal" }).getAttribute("aria-pressed")).toBe("true");
    expect(within(sheet).queryByText("N")).toBeNull(); // kept for the agents, left out of the sheet
    fireEvent.click(within(sheet).getByRole("button", { name: "Undo: Stage: Lead → Proposal" }));
    await waitFor(() => expect(api.write).toHaveBeenCalledTimes(1));
    expect(api.write.mock.calls[0]![1]).toEqual([{ op: "revert", change_id: "01J8ZQ4Y7Z5G0000000000CHG2", version: 2 }]);
    await waitFor(() => expect(api.record).toHaveBeenCalledTimes(2)); // read again after the write
    fireEvent.click(within(sheet).getByRole("button", { name: "Move to Lost" }));
    await waitFor(() => expect(api.write).toHaveBeenCalledTimes(2));
    expect(api.write.mock.calls[1]![1]).toEqual([{ op: "set", id: ids.acme, version: 2, fields: { stage: "lost" } }]);
    await waitFor(() => expect(screen.queryByTestId("record-sheet")).toBeNull());
  });

  it("adds by the view's fields, the required ones first, and asks «Add anyway» of one that may be there", async () => {
    const form = {
      name: "followups", rev: REV, options: { deal: [{ id: ids.acme, title: "Acme · Widgets" }] },
      fields: { deal: { type: "ref", to: "deals", required: true }, date: { type: "date", required: true }, state: { type: "enum", values: ["draft", "done"] }, note: { type: "text" } },
    };
    const duplicate = refused({ kind: "duplicate", record: { id: ids.f1, fields: { deal: ids.acme, note: "Call back" }, version: 1, created: 0, updated: 0, deleted: null } }, 400);
    const api = mount({ form: vi.fn().mockResolvedValue(form), write: vi.fn().mockRejectedValueOnce(duplicate).mockResolvedValueOnce(answered("followups", ids.f2, 1)) });
    fireEvent.click(await screen.findByRole("button", { name: "Add a follow-up" }));
    const sheet = await screen.findByTestId("add-sheet");
    await within(sheet).findByText("Deal");
    expect(within(sheet).queryByText("Note (optional)")).toBeNull(); // not among the view's fields
    expect((within(sheet).getByDisplayValue("2026-09-16") as HTMLInputElement).type).toBe("date"); // today, required
    const add = within(sheet).getByRole("button", { name: "Add" }) as HTMLButtonElement;
    expect(add.disabled).toBe(true);
    fireEvent.change(within(sheet).getAllByRole("combobox")[0]!, { target: { value: ids.acme } });
    expect(add.disabled).toBe(false);
    fireEvent.click(add);
    const asked = await screen.findByTestId("conflict");
    expect(asked.textContent).toContain("It may be the same as “Call back”");
    expect(api.write.mock.calls[0]![1]).toEqual([{ op: "add", fields: { deal: ids.acme, date: "2026-09-16", state: "draft" } }]);
    const anyway = await screen.findByRole("button", { name: "Add anyway" }); // drawn a tick after the card
    await act(async () => fireEvent.click(anyway));
    await waitFor(() => expect(api.write).toHaveBeenCalledTimes(2));
    expect(api.write.mock.calls[1]![1]).toEqual([{ op: "add", fields: { deal: ids.acme, date: "2026-09-16", state: "draft" }, distinct: true }]);
    await waitFor(() => expect(screen.queryByTestId("add-sheet")).toBeNull());
  });
});

describe("the words of a write", () => {
  it("says who changed what, from what to what", () => {
    expect(["owner", "assistant", "session:s1", "mac:air/s2", "code:mail-sent", "import:X", "define:3"].map(actorWords)).toEqual([
      "you", "the assistant", "a session", "the Mac", "the rule mail-sent", "an import", "a form change",
    ]);
    const fields = { deal: { type: "ref" as const, to: "deals" }, stage: { type: "enum" as const } };
    expect(changeWords({ op: "set", before: { stage: "pack_ready", deal: null }, after: { stage: "interview", deal: ids.acme } }, fields, { [ids.acme]: "Acme" })).toEqual([
      "Stage: Pack ready → Interview", "Deal: Acme",
    ]);
    expect(changeWords({ op: "revert", before: { _deleted: null }, after: { _deleted: 5 } })).toEqual(["undone", "deleted"]);
    expect(changeWords({ op: "add", before: null, after: { a: 1 } })).toEqual(["added"]);
    expect(valueText(ids.globex)).toBe("another record");
    expect(valueText(true)).toBe("yes");
    expect(conflictWords({ error: "", kind: "stale", record: null, change: null })).toBe("It changed since you saw it.");
  });

  it("writes again at the record's new version, and takes a write back change by change", () => {
    const steps = [
      { op: { op: "set" as const, id: ids.acme, version: 1, fields: { a: 1 } }, target: ids.acme },
      { op: { op: "revert" as const, change_id: REV, version: 1 }, target: ids.globex },
    ];
    const record = { id: ids.globex, fields: {}, version: 7, created: 0, updated: 0, deleted: null };
    expect(againAt(steps, { error: "", kind: "stale", record, change: null }).map((s) => ("version" in s.op ? s.op.version : null))).toEqual([1, 7]);
    const answer: WriteAnswer = { name: "deals", rev: REV, change_ids: [REV, null], records: [{ ...record, id: ids.acme, version: 2 }, record] };
    expect(undoSteps(answer)).toEqual([{ op: { op: "revert", change_id: REV, version: 2 }, target: ids.acme }]);
  });

  it("sends only the fields given, an int as a number; starts a required date at today", () => {
    const specs = { n: { type: "int" as const }, note: { type: "text" as const }, ok: { type: "bool" as const }, day: { type: "date" as const, required: true }, at: { type: "date" as const } };
    expect(addFields({ n: "12", note: "  hi ", ok: false, day: "2026-09-16", at: "" }, specs)).toEqual({ n: 12, note: "hi", day: "2026-09-16" });
    expect(startValues(["n", "ok", "day", "at"], specs, { n: 3 }, "2026-09-16")).toEqual({ n: "3", ok: false, day: "2026-09-16", at: "" });
  });
});
