import { describe, expect, it } from "vitest";
import {
  type Action,
  groupSessions,
  initialState,
  lastWords,
  orderedFiles,
  orderedInputs,
  reduce,
  type State,
  sessionApprovals,
  orderedSummaries,
  sessionItems,
  shownApprovals,
  waitingCount,
} from "./store";
import type { Approval, Input, Item, Session, StreamEvent } from "./types";

const session = (id: string, over: Partial<Session> = {}): Session => ({
  id,
  track: "code",
  lane: "code",
  kind: "code",
  state: "active",
  created: 1000,
  at: 1000,
  title: "Fix the tests",
  waiting: 0,
  ...over,
});

const message = (s: string, turn: number, n: number, text: string): Item => ({
  session: s,
  turn,
  n,
  kind: "message",
  data: { role: "assistant", text },
});

const approval = (id: string, over: Partial<Approval> = {}): Approval => ({
  id,
  session: "s1",
  kind: "approval",
  tool_use_id: "toolu_1",
  state: "shown",
  tool: "Bash",
  input: { command: "make test" },
  cwd: "/work/retinue",
  digest: "d1",
  view: { head: ["Approval · code · session s1"], body: "make test" },
  created: 10,
  at: 10,
  ...over,
});

const ev = (id: number | null, event: StreamEvent): Action => ({ type: "event", id, event });
const run = (actions: Action[], from: State = initialState) => actions.reduce(reduce, from);

describe("reduce", () => {
  it("takes the stream cursor from the first snapshot and advances it with event ids", () => {
    let st = run([{ type: "list", snapshot: { schema: 1, cursor: 40, sessions: [session("s1")] } }]);
    expect(st.cursor).toBe(40);
    st = run([ev(41, { kind: "session", data: session("s1", { state: "idle" }) })], st);
    expect(st.cursor).toBe(41);
    expect(st.sessions.s1?.state).toBe("idle");
  });

  it("upserts items by <session>/<turn>/<n> and keeps (turn, n) order", () => {
    const st = run([
      ev(1, { kind: "item", data: message("s1", 1, 0, "second turn") }),
      ev(2, { kind: "item", data: message("s1", 0, 1, "b") }),
      ev(3, { kind: "item", data: message("s1", 0, 0, "a") }),
      ev(4, { kind: "item", data: message("s1", 0, 1, "b, corrected") }),
    ]);
    expect(sessionItems(st, "s1").map((i) => (i.kind === "message" ? i.data.text : ""))).toEqual([
      "a",
      "b, corrected",
      "second turn",
    ]);
  });

  it("items_reset drops a turn's items with n >= count and leaves other turns", () => {
    const st = run([
      ev(1, { kind: "item", data: message("s1", 0, 0, "a") }),
      ev(2, { kind: "item", data: message("s1", 0, 1, "b") }),
      ev(3, { kind: "item", data: message("s1", 0, 2, "c") }),
      ev(4, { kind: "item", data: message("s1", 1, 5, "next turn") }),
      ev(5, { kind: "items_reset", data: { session: "s1", turn: 0, count: 1 } }),
    ]);
    expect(sessionItems(st, "s1").map((i) => `${i.turn}/${i.n}`)).toEqual(["0/0", "1/5"]);
  });

  it("upserts approvals by id and keeps every summary of a session, ordered by time", () => {
    const st = run([
      ev(1, { kind: "approval", data: approval("a1") }),
      ev(2, { kind: "approval", data: approval("a1", { state: "granted", at: 11 }) }),
      ev(3, { kind: "summary", data: { session: "s1", seq: 7, head: "Result", text: "two", at: 2 } }),
      ev(4, { kind: "summary", data: { session: "s1", seq: 5, head: "Result", text: "one", at: 1 } }),
      ev(5, { kind: "summary", data: { session: "s1", seq: 7, head: "Result", text: "two, corrected", at: 2 } }),
    ]);
    expect(st.approvals.a1?.state).toBe("granted");
    expect(orderedSummaries(st.summaries.s1).map((x) => x.text)).toEqual(["one", "two, corrected"]);
    expect(shownApprovals(st.approvals)).toEqual([]);
  });

  it("a decide response (no event id) never replaces a newer copy from the stream", () => {
    const st = run([
      ev(1, { kind: "approval", data: approval("a1", { state: "executed", at: 30 }) }),
      { type: "event", id: null, event: { kind: "approval", data: approval("a1", { state: "granted", at: 20 }) } },
    ]);
    expect(st.approvals.a1?.state).toBe("executed");
    const fresh = run([{ type: "event", id: null, event: { kind: "approval", data: approval("a1", { state: "granted", at: 20 }) } }]);
    expect(fresh.approvals.a1?.state).toBe("granted");
  });

  it("a snapshot older than events already applied keeps those events (the request raced the stream)", () => {
    const old = { schema: 1, cursor: 50 };
    const st = run([
      ev(51, { kind: "item", data: message("s1", 0, 0, "newer than the snapshot") }),
      ev(52, { kind: "item", data: message("s1", 0, 3, "came after the snapshot") }),
      ev(53, { kind: "approval", data: approval("a1", { state: "granted", at: 40 }) }),
      ev(54, { kind: "items_reset", data: { session: "s1", turn: 1, count: 1 } }),
      ev(55, { kind: "summary", data: { session: "s1", seq: 9, head: "Result", text: "after the snapshot", at: 9 } }),
      {
        type: "session",
        snapshot: {
          ...old,
          session: session("s1", { state: "waiting", waiting: 1 }),
          items: [message("s1", 0, 0, "from the snapshot"), message("s1", 1, 0, "a"), message("s1", 1, 1, "dropped after the snapshot")],
          approvals: [approval("a1", { state: "shown" }), approval("a2", { state: "shown" })],
          summaries: [{ session: "s1", seq: 8, head: "Result", text: "from the snapshot", at: 8 }],
        },
      },
    ]);
    const texts = sessionItems(st, "s1").map((i) => `${i.turn}/${i.n} ${i.kind === "message" ? i.data.text : ""}`);
    expect(texts).toEqual(["0/0 newer than the snapshot", "0/3 came after the snapshot", "1/0 a"]);
    expect(st.approvals.a1?.state).toBe("granted");
    expect(st.approvals.a2?.state).toBe("shown");
    expect(orderedSummaries(st.summaries.s1).map((x) => x.text)).toEqual(["from the snapshot", "after the snapshot"]);
    // and a later snapshot that already holds those events takes over completely
    const later = run(
      [{ type: "session", snapshot: { schema: 1, cursor: 60, session: session("s1"), items: [], approvals: [], summaries: [] } }],
      st,
    );
    expect(sessionItems(later, "s1")).toEqual([]);
    expect(later.approvals.a1).toBeUndefined();
  });

  it("appends deltas and drops them when the final message item arrives", () => {
    let st = run([
      { type: "delta", session: "s1", text: "Looking at " },
      { type: "delta", session: "s1", text: "the tests" },
    ]);
    expect(st.deltas.s1).toBe("Looking at the tests");
    // a delta carries no id: the cursor does not move
    expect(st.cursor).toBeNull();
    st = run([ev(7, { kind: "item", data: message("s1", 0, 0, "Looking at the tests.") })], st);
    expect(st.deltas.s1).toBeUndefined();
  });

  it("drops the delta when the session stops running", () => {
    const st = run([
      { type: "delta", session: "s1", text: "…" },
      ev(1, { kind: "session", data: session("s1", { state: "idle" }) }),
    ]);
    expect(st.deltas.s1).toBeUndefined();
  });

  it("skips events already contained in a session snapshot", () => {
    const st = run([
      {
        type: "session",
        snapshot: {
          schema: 1,
          cursor: 50,
          session: session("s1", { state: "waiting", waiting: 1 }),
          items: [message("s1", 0, 0, "new")],
          approvals: [approval("a1")],
          summaries: [],
        },
      },
      ev(49, { kind: "item", data: message("s1", 0, 0, "old") }),
      ev(48, { kind: "session", data: session("s1", { state: "active" }) }),
      ev(51, { kind: "approval", data: approval("a1", { state: "granted" }) }),
    ]);
    expect(sessionItems(st, "s1")[0]?.kind === "message" && sessionItems(st, "s1")[0]?.data).toMatchObject({
      text: "new",
    });
    expect(st.sessions.s1?.state).toBe("waiting");
    expect(st.approvals.a1?.state).toBe("granted");
    expect(st.approvals.a1?.session).toBe("s1");
    expect(sessionApprovals(st, "s1")).toHaveLength(1);
  });

  it("a session snapshot replaces that session's items and approvals only", () => {
    const st = run([
      ev(1, { kind: "item", data: message("s1", 0, 0, "x") }),
      ev(2, { kind: "item", data: message("s2", 0, 0, "y") }),
      ev(3, { kind: "approval", data: approval("gone", { session: "s1" }) }),
      ev(4, { kind: "approval", data: approval("other", { session: "s2" }) }),
      {
        type: "session",
        snapshot: { schema: 1, cursor: 10, session: session("s1"), items: [], approvals: [], summaries: [] },
      },
    ]);
    expect(sessionItems(st, "s1")).toEqual([]);
    expect(sessionItems(st, "s2")).toHaveLength(1);
    expect(st.approvals.gone).toBeUndefined();
    expect(st.approvals.other).toBeDefined();
  });

  it("a list snapshot keeps rows newer than itself and loaded sessions it no longer names", () => {
    const st = run([
      { type: "list", snapshot: { schema: 1, cursor: 5, sessions: [session("s1"), session("old")] } },
      {
        type: "session",
        snapshot: { schema: 1, cursor: 6, session: session("old", { state: "closed" }), items: [], approvals: [], summaries: [] },
      },
      ev(9, { kind: "session", data: session("s1", { state: "waiting", waiting: 1 }) }),
      { type: "list", snapshot: { schema: 1, cursor: 8, sessions: [session("s1", { state: "active" })] } },
    ]);
    expect(st.sessions.s1?.state).toBe("waiting");
    expect(st.sessions.old?.state).toBe("closed");
  });

  it("converges: the same events applied twice give the same state", () => {
    const events: Action[] = [
      ev(1, { kind: "session", data: session("s1") }),
      ev(2, { kind: "item", data: message("s1", 0, 0, "a") }),
      ev(3, { kind: "approval", data: approval("a1") }),
    ];
    const once = run(events);
    expect(run(events, once)).toEqual(once);
  });
});

describe("groups", () => {
  const now = 100 * 86400;
  it("puts sessions under «Waiting for you», «Working», «Done», newest first", () => {
    const g = groupSessions(
      [
        session("w", { state: "waiting", waiting: 2, at: now - 10 }),
        session("a1", { state: "active", at: now - 30 }),
        session("a2", { state: "active", at: now - 20 }),
        session("i", { state: "idle", at: now - 40 }),
        session("c", { state: "closed", at: now - 86400 }),
        session("ancient", { state: "closed", at: now - 30 * 86400 }),
      ],
      now,
    );
    expect(g.waiting.map((s) => s.id)).toEqual(["w"]);
    expect(g.active.map((s) => s.id)).toEqual(["a2", "a1"]);
    expect(g.done.map((s) => s.id)).toEqual(["i", "c"]);
  });

  it("puts pinned sessions in their own group on top, but a waiting one stays with its cards", () => {
    const now = 2_000_000;
    const g = groupSessions(
      [
        session("p", { state: "idle", at: now - 50, pinned: true }),
        session("old", { state: "closed", at: now - 30 * 86400, pinned: true }),
        session("pw", { state: "waiting", waiting: 1, at: now - 5, pinned: true }),
        session("a", { state: "active", at: now - 10 }),
      ],
      now,
    );
    expect(g.waiting.map((s) => s.id)).toEqual(["pw"]);
    expect(g.pinned.map((s) => s.id)).toEqual(["p", "old"]); // pinned: kept even when closed long ago
    expect(g.active.map((s) => s.id)).toEqual(["a"]);
    expect(g.done).toEqual([]);
  });

  it("hangs side sessions under the session they ask about, unless they wait or it is not listed", () => {
    const now = 2_000_000;
    const g = groupSessions(
      [
        session("main", { state: "idle", at: now - 100 }),
        session("q1", { state: "active", at: now - 10, parent: "main" }),
        session("q2", { state: "idle", at: now - 5, parent: "main" }),
        session("q3", { state: "waiting", waiting: 1, at: now - 1, parent: "main" }),
        session("orphan", { state: "idle", at: now - 20, parent: "gone" }),
      ],
      now,
    );
    expect(g.sides.main?.map((s) => s.id)).toEqual(["q2", "q1"]);
    expect(g.waiting.map((s) => s.id)).toEqual(["q3"]);
    expect(g.active).toEqual([]);
    expect(g.done.map((s) => s.id)).toEqual(["orphan", "main"]);
  });

  it("peeks at the text being written, else the last model message, else the last summary", () => {
    const items = [message("s", 0, 0, "first"), message("s", 0, 1, "last")];
    expect(lastWords(items, "writing…", [])).toBe("writing…");
    expect(lastWords(items, undefined, [])).toBe("last");
    expect(lastWords([], "", [{ session: "s", head: "Result", text: "result", at: 1 }])).toBe("result");
    expect(lastWords([], undefined, [])).toBe("");
  });

  it("keeps a newer stream row over a pin's response that comes after it", () => {
    let st = reduce(initialState, { type: "list", snapshot: { schema: 1, cursor: 5, sessions: [session("s", { at: 10 })] } });
    st = reduce(st, { type: "event", id: 6, event: { kind: "session", data: session("s", { at: 20, state: "waiting", waiting: 1 }) } });
    st = reduce(st, { type: "event", id: null, event: { kind: "session", data: session("s", { at: 10, pinned: true }) } });
    expect(st.sessions.s?.state).toBe("waiting");
    st = reduce(st, { type: "event", id: null, event: { kind: "session", data: session("s", { at: 20, state: "waiting", waiting: 1, pinned: true }) } });
    expect(st.sessions.s?.pinned).toBe(true);
  });

  it("counts waiting cards for the title", () => {
    expect(waitingCount([session("w", { state: "waiting", waiting: 2 }), session("a")])).toBe(2);
  });
});

describe("the owner's messages", () => {
  const input = (id: string, text: string, at: number, over: Partial<Input> = {}): Input => ({
    session: "s1",
    input_id: id,
    text,
    at,
    turn: 0,
    n: 0,
    attachments: [],
    ...over,
  });

  it("are upserted by input_id from the stream and merged with a snapshot, the newer side winning", () => {
    const photo = { kind: "image", name: "", size: 10, what: "photo 40×20", thumb: true };
    const st = run([
      ev(51, { kind: "input", data: input("face:b", "after the snapshot", 2, { attachments: [photo] }) }),
      {
        type: "session",
        snapshot: {
          schema: 1,
          cursor: 50,
          session: session("s1"),
          items: [],
          approvals: [],
          summaries: [],
          inputs: [input("tg:1", "from the snapshot", 1)],
        },
      },
    ]);
    expect(orderedInputs(st.inputs.s1).map((x) => [x.input_id, x.text, x.attachments.length])).toEqual([
      ["tg:1", "from the snapshot", 0],
      ["face:b", "after the snapshot", 1],
    ]);
    const old = run([{ type: "session", snapshot: { schema: 1, cursor: 9, session: session("s2"), items: [], approvals: [], summaries: [] } }]);
    expect(orderedInputs(old.inputs.s2)).toEqual([]);
  });
});

describe("inputs, files and questions together", () => {
  it("fold side by side from the stream and from a snapshot", () => {
    const mine: Input = { session: "s1", input_id: "tg:2", text: "here is the log", at: 3, turn: 1, n: 0, attachments: [] };
    const file = { id: "f1", session: "s1", path: "report.pdf", name: "report.pdf", size: 10, mime: "application/pdf", at: 4 };
    const question = approval("q1", { kind: "question", tool: "AskUserQuestion" });
    const st = run([
      { type: "session", snapshot: { schema: 1, cursor: 10, session: session("s1"), items: [], approvals: [], summaries: [], inputs: [], files: [] } },
      ev(11, { kind: "input", data: mine }),
      ev(12, { kind: "file", data: file }),
      ev(13, { kind: "approval", data: question }),
    ]);
    expect(orderedInputs(st.inputs.s1).map((x) => x.input_id)).toEqual(["tg:2"]);
    expect(orderedFiles(st.files.s1).map((f) => f.id)).toEqual(["f1"]);
    expect(st.approvals.q1?.kind).toBe("question");
    const again = run([{ type: "session", snapshot: { schema: 1, cursor: 13, session: session("s1"), items: [], approvals: [question], summaries: [], inputs: [mine], files: [file] } }], st);
    expect([orderedInputs(again.inputs.s1).length, orderedFiles(again.files.s1).length, again.approvals.q1?.kind]).toEqual([1, 1, "question"]);
  });
});
