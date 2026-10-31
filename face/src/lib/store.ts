// The one event store of the app: snapshots and stream events folded by a pure reducer. Every stream event is an
// upsert of one whole object by key (the spec's table), so applying them in order converges on the server's state.
//
// A snapshot is taken at a cursor: it already holds every event up to that id. Events of a session at or below the
// cursor of that session's snapshot (or of the list snapshot, for the session row) are skipped. The other way round,
// a snapshot that arrives after newer events (it was requested before them) keeps those events: each item, reset,
// approval and summary remembers the id of the event that set it, and what is newer than the snapshot's cursor wins.

import type {
  About,
  Approval,
  BaseCard,
  Input,
  Item,
  OutFile,
  PushCard,
  Session,
  SessionSnapshot,
  SessionsSnapshot,
  StreamEvent,
  Summary,
} from "./types";
import { hideReplyLines } from "./replies";

export interface State {
  /** The id of the last stream event seen: where the stream resumes. Null before the first snapshot. */
  cursor: number | null;
  /** Per session: the cursor of its loaded snapshot. A session is "loaded" when it has an entry. */
  floors: Record<string, number>;
  sessions: Record<string, Session>;
  /** Per session row: the cursor it is as of (a snapshot's cursor or an event's id). */
  rowAt: Record<string, number>;
  /** Per session: items keyed `<turn>/<n>`. */
  items: Record<string, Record<string, Item>>;
  /** Per session, per item key: the id of the event that last set it (only those newer than a snapshot matter). */
  itemAt: Record<string, Record<string, number>>;
  /** Per session, per turn: the last `items_reset` seen on the stream. */
  resets: Record<string, Record<string, { id: number; count: number }>>;
  approvals: Record<string, Approval>;
  /** Per approval: the id of the event that last set it. */
  approvalAt: Record<string, number>;
  /** Per session: every summary, keyed by its note number (or its time when the server sends none). */
  summaries: Record<string, Record<string, Summary>>;
  summaryAt: Record<string, Record<string, number>>;
  /** Per session: the owner's messages, keyed by input_id. */
  inputs: Record<string, Record<string, Input>>;
  inputAt: Record<string, Record<string, number>>;
  /** Per session: the files it handed over, by id. A file never changes once handed over. */
  files: Record<string, Record<string, OutFile>>;
  fileAt: Record<string, Record<string, number>>;
  /** The live text of a running turn, not stored by the server; the final item replaces it. */
  deltas: Record<string, string>;
  /** «To the Base» cards by id, and the id of the event that last set each. */
  bases: Record<string, BaseCard>;
  baseAt: Record<string, number>;
  /** «Push» cards by id, the same way. */
  pushes: Record<string, PushCard>;
  pushAt: Record<string, number>;
  /** Per session: «Summary»'s environment, as of its last snapshot (not on the stream). */
  about: Record<string, About>;
}

export const initialState: State = {
  cursor: null,
  floors: {},
  sessions: {},
  rowAt: {},
  items: {},
  itemAt: {},
  resets: {},
  approvals: {},
  approvalAt: {},
  summaries: {},
  summaryAt: {},
  inputs: {},
  inputAt: {},
  files: {},
  fileAt: {},
  deltas: {},
  bases: {},
  baseAt: {},
  pushes: {},
  pushAt: {},
  about: {},
};

export type Action =
  | { type: "list"; snapshot: SessionsSnapshot }
  | { type: "session"; snapshot: SessionSnapshot }
  | { type: "event"; id: number | null; event: StreamEvent }
  | { type: "delta"; session: string; text: string }
  | { type: "reset" };

export const itemKey = (turn: number, n: number) => `${turn}/${n}`;
export const summaryKey = (s: Summary) => String(s.seq ?? s.at);

function sessionOf(event: StreamEvent): string {
  return event.kind === "session" ? event.data.id : event.data.session;
}

function without<T>(record: Record<string, T>, key: string): Record<string, T> {
  if (!(key in record)) return record;
  const { [key]: _gone, ...rest } = record;
  return rest;
}

/** Set `record[outer][inner] = value` without touching the rest. */
function put2<T>(record: Record<string, Record<string, T>>, outer: string, inner: string, value: T) {
  return { ...record, [outer]: { ...(record[outer] ?? {}), [inner]: value } };
}

function applyEvent(state: State, event: StreamEvent, id: number | null): State {
  switch (event.kind) {
    case "session": {
      const row = event.data;
      const prior = state.sessions[row.id];
      // A copy without an event id (a pin's response) never replaces a newer row from the stream.
      if (id === null && prior && prior.at > row.at) return state;
      const deltas = row.state === "active" ? state.deltas : without(state.deltas, row.id);
      const rowAt = id === null ? state.rowAt : { ...state.rowAt, [row.id]: id };
      return { ...state, sessions: { ...state.sessions, [row.id]: row }, rowAt, deltas };
    }
    case "item": {
      const item = event.data;
      const key = itemKey(item.turn, item.n);
      const final = item.kind === "message" || item.kind === "result";
      return {
        ...state,
        items: put2(state.items, item.session, key, item),
        itemAt: id === null ? state.itemAt : put2(state.itemAt, item.session, key, id),
        deltas: final ? without(state.deltas, item.session) : state.deltas,
      };
    }
    case "items_reset": {
      const { session, turn, count } = event.data;
      const mine = state.items[session] ?? {};
      const kept = Object.fromEntries(Object.entries(mine).filter(([, it]) => it.turn !== turn || it.n < count));
      return {
        ...state,
        items: { ...state.items, [session]: kept },
        resets: id === null ? state.resets : put2(state.resets, session, String(turn), { id, count }),
      };
    }
    case "approval": {
      const a = event.data;
      const prior = state.approvals[a.id];
      // A copy without an event id (a decide response) never replaces a newer one from the stream.
      if (id === null && prior && prior.at > a.at) return state;
      return {
        ...state,
        approvals: { ...state.approvals, [a.id]: a },
        approvalAt: id === null ? state.approvalAt : { ...state.approvalAt, [a.id]: id },
      };
    }
    case "summary": {
      const sum = event.data;
      const key = summaryKey(sum);
      return {
        ...state,
        summaries: put2(state.summaries, sum.session, key, sum),
        summaryAt: id === null ? state.summaryAt : put2(state.summaryAt, sum.session, key, id),
      };
    }
    case "input": {
      const input = event.data;
      return {
        ...state,
        inputs: put2(state.inputs, input.session, input.input_id, input),
        inputAt: id === null ? state.inputAt : put2(state.inputAt, input.session, input.input_id, id),
      };
    }
    case "file": {
      const file = event.data;
      return {
        ...state,
        files: put2(state.files, file.session, file.id, file),
        fileAt: id === null ? state.fileAt : put2(state.fileAt, file.session, file.id, id),
      };
    }
    case "base": {
      const card = event.data;
      const prior = state.bases[card.id];
      // A copy without an event id (an answer to a press) never replaces a newer one from the stream.
      if (id === null && prior && prior.at > card.at) return state;
      return {
        ...state,
        bases: { ...state.bases, [card.id]: card },
        baseAt: id === null ? state.baseAt : { ...state.baseAt, [card.id]: id },
      };
    }
    case "push": {
      const card = event.data;
      const prior = state.pushes[card.id];
      if (id === null && prior && prior.at > card.at) return state;
      return {
        ...state,
        pushes: { ...state.pushes, [card.id]: card },
        pushAt: id === null ? state.pushAt : { ...state.pushAt, [card.id]: id },
      };
    }
  }
}

/** Entries of `at` newer than `cursor`: what the stream brought after a snapshot was taken. */
function newer(at: Record<string, number> | undefined, cursor: number): Record<string, number> {
  return Object.fromEntries(Object.entries(at ?? {}).filter(([, id]) => id > cursor));
}

/** A session's snapshot merged with what the stream already brought after its cursor: the newer side wins per key. */
function applySnapshot(state: State, snapshot: SessionSnapshot): State {
  const id = snapshot.session.id;
  const c = snapshot.cursor;

  const itemAt = newer(state.itemAt[id], c);
  const items: Record<string, Item> = Object.fromEntries(
    snapshot.items.map((it) => [itemKey(it.turn, it.n), { ...it, session: id }]),
  );
  for (const key of Object.keys(itemAt)) {
    const it = state.items[id]?.[key];
    if (it) items[key] = it;
  }
  const resets = Object.fromEntries(Object.entries(state.resets[id] ?? {}).filter(([, r]) => r.id > c));
  for (const [turn, r] of Object.entries(resets)) {
    for (const [key, it] of Object.entries(items)) {
      if (String(it.turn) === turn && it.n >= r.count && (itemAt[key] ?? 0) < r.id) delete items[key];
    }
  }

  const approvalAt = { ...state.approvalAt };
  const approvals: Record<string, Approval> = {};
  for (const [key, a] of Object.entries(state.approvals)) {
    if (a.session !== id || (approvalAt[key] ?? 0) > c) approvals[key] = a;
    else delete approvalAt[key];
  }
  for (const a of snapshot.approvals) {
    if ((approvalAt[a.id] ?? 0) <= c) approvals[a.id] = { ...a, session: a.session || id };
  }

  const summaryAt = newer(state.summaryAt[id], c);
  const summaries: Record<string, Summary> = Object.fromEntries(snapshot.summaries.map((x) => [summaryKey(x), x]));
  for (const key of Object.keys(summaryAt)) {
    const x = state.summaries[id]?.[key];
    if (x) summaries[key] = x;
  }

  const inputAt = newer(state.inputAt[id], c);
  const inputs: Record<string, Input> = Object.fromEntries((snapshot.inputs ?? []).map((x) => [x.input_id, x]));
  for (const key of Object.keys(inputAt)) {
    const x = state.inputs[id]?.[key];
    if (x) inputs[key] = x;
  }

  const fileAt = newer(state.fileAt[id], c);
  const files: Record<string, OutFile> = Object.fromEntries((snapshot.files ?? []).map((f) => [f.id, f]));
  for (const key of Object.keys(fileAt)) {
    const f = state.files[id]?.[key];
    if (f) files[key] = f;
  }

  const baseAt = { ...state.baseAt };
  const bases: Record<string, BaseCard> = {};
  for (const [key, b] of Object.entries(state.bases)) {
    if (b.session !== id || (baseAt[key] ?? 0) > c) bases[key] = b;
    else delete baseAt[key];
  }
  for (const b of snapshot.base_cards ?? []) {
    if ((baseAt[b.id] ?? 0) <= c) bases[b.id] = b;
  }

  const pushAt = { ...state.pushAt };
  const pushes: Record<string, PushCard> = {};
  for (const [key, p] of Object.entries(state.pushes)) {
    if (p.session !== id || (pushAt[key] ?? 0) > c) pushes[key] = p;
    else delete pushAt[key];
  }
  for (const p of snapshot.push_cards ?? []) {
    if ((pushAt[p.id] ?? 0) <= c) pushes[p.id] = p;
  }

  return {
    ...state,
    bases,
    baseAt,
    pushes,
    pushAt,
    about: snapshot.about ? { ...state.about, [id]: snapshot.about } : state.about,
    floors: { ...state.floors, [id]: c },
    sessions: (state.rowAt[id] ?? 0) > c ? state.sessions : { ...state.sessions, [id]: snapshot.session },
    rowAt: { ...state.rowAt, [id]: Math.max(state.rowAt[id] ?? 0, c) },
    items: { ...state.items, [id]: items },
    itemAt: { ...state.itemAt, [id]: itemAt },
    resets: { ...state.resets, [id]: resets },
    approvals,
    approvalAt,
    summaries: { ...state.summaries, [id]: summaries },
    summaryAt: { ...state.summaryAt, [id]: summaryAt },
    inputs: { ...state.inputs, [id]: inputs },
    inputAt: { ...state.inputAt, [id]: inputAt },
    files: { ...state.files, [id]: files },
    fileAt: { ...state.fileAt, [id]: fileAt },
    cursor: state.cursor === null ? c : state.cursor,
  };
}

export function reduce(state: State, action: Action): State {
  switch (action.type) {
    case "reset":
      return initialState;
    case "list": {
      const { snapshot } = action;
      const sessions: Record<string, Session> = Object.fromEntries(snapshot.sessions.map((s) => [s.id, s]));
      const rowAt: Record<string, number> = Object.fromEntries(snapshot.sessions.map((s) => [s.id, snapshot.cursor]));
      for (const [id, row] of Object.entries(state.sessions)) {
        const at = state.rowAt[id] ?? 0;
        // A row newer than the list (from a session snapshot or the stream) stays; so does a loaded session the
        // list no longer names (an old closed one opened by its link).
        if (at > snapshot.cursor || (!sessions[id] && id in state.floors)) {
          sessions[id] = row;
          rowAt[id] = at;
        }
      }
      return { ...state, sessions, rowAt, cursor: state.cursor === null ? snapshot.cursor : state.cursor };
    }
    case "session":
      return applySnapshot(state, action.snapshot);
    case "event": {
      const { id, event } = action;
      const cursor = id === null ? state.cursor : Math.max(state.cursor ?? 0, id);
      if (id !== null) {
        const floors = event.kind === "session" ? state.rowAt : state.floors;
        if (id <= (floors[sessionOf(event)] ?? 0)) return { ...state, cursor };
      }
      return { ...applyEvent(state, event, id), cursor };
    }
    case "delta": {
      const prior = state.deltas[action.session] ?? "";
      return { ...state, deltas: { ...state.deltas, [action.session]: prior + action.text } };
    }
  }
}

// --- selectors --------------------------------------------------------------------------------------------------

/** A session's items in (turn, n) order; pass `state.items[session]`. */
export function orderedItems(items: Record<string, Item> | undefined): Item[] {
  return Object.values(items ?? {}).sort((a, b) => a.turn - b.turn || a.n - b.n);
}

/** A session's cards, oldest first; pass `state.approvals`. */
export function approvalsOf(approvals: Record<string, Approval>, session: string): Approval[] {
  return Object.values(approvals)
    .filter((a) => a.session === session)
    .sort((a, b) => a.created - b.created);
}

/** A session's summaries, oldest first; pass `state.summaries[session]`. */
export function orderedSummaries(summaries: Record<string, Summary> | undefined): Summary[] {
  return Object.values(summaries ?? {}).sort((a, b) => a.at - b.at);
}

/** A session's messages from the owner, oldest first; pass `state.inputs[session]`. */
export function orderedInputs(inputs: Record<string, Input> | undefined): Input[] {
  return Object.values(inputs ?? {}).sort((a, b) => a.at - b.at);
}

/** A session's files, newest first; pass `state.files[session]`. */
export function orderedFiles(files: Record<string, OutFile> | undefined): OutFile[] {
  return Object.values(files ?? {}).sort((a, b) => b.at - a.at || b.path.localeCompare(a.path));
}

export const sessionItems = (state: State, session: string) => orderedItems(state.items[session]);
export const sessionApprovals = (state: State, session: string) => approvalsOf(state.approvals, session);

/** A session's «To the Base» card still open (shown or committing), the newest; pass `state.bases`. */
export function openBaseCard(bases: Record<string, BaseCard>, session: string): BaseCard | undefined {
  return Object.values(bases)
    .filter((b) => b.session === session && (b.state === "shown" || b.state === "committing"))
    .sort((a, b) => b.created - a.created)[0];
}

/** A card that waits on the owner now: a call to decide or a question to answer. */
export const waitsOnOwner = (a: Approval) => (a.kind === "approval" || a.kind === "question") && a.state === "shown";

/** Cards the owner can press now, oldest first; pass `state.approvals`. */
export function shownApprovals(approvals: Record<string, Approval>): Approval[] {
  return Object.values(approvals)
    .filter(waitsOnOwner)
    .sort((a, b) => a.created - b.created);
}

export interface Groups {
  waiting: Session[];
  pinned: Session[];
  active: Session[];
  done: Session[];
  /** Side sessions by the session they ask about, newest first: listed under it, not in a group of their own. */
  sides: Record<string, Session[]>;
}

export const RECENT_S = 7 * 86400;

const isWaiting = (s: Session) => s.state === "waiting" || s.waiting > 0;

/** «Waiting for you» / «Pinned» / «Working» / «Done» (idle, and closed in the last week), newest first. A pinned
 * session that waits stays under «Waiting for you» with its cards; a side session whose main session is listed goes under
 * it — unless it waits on a card itself, which always comes to the top. */
export function groupSessions(sessions: Session[], now: number): Groups {
  const recent = (s: Session) => s.state !== "closed" || now - s.at < RECENT_S;
  const listed = [...sessions].sort((a, b) => b.at - a.at).filter((s) => isWaiting(s) || s.pinned || recent(s));
  const ids = new Set(listed.map((s) => s.id));
  const sides: Record<string, Session[]> = {};
  const top: Session[] = [];
  for (const s of listed) {
    if (s.parent && ids.has(s.parent) && !isWaiting(s)) sides[s.parent] = [...(sides[s.parent] ?? []), s];
    else top.push(s);
  }
  const free = top.filter((s) => !isWaiting(s) && !s.pinned);
  return {
    waiting: top.filter(isWaiting),
    pinned: top.filter((s) => !isWaiting(s) && s.pinned),
    active: free.filter((s) => s.state === "active"),
    done: free.filter((s) => s.state !== "active"),
    sides,
  };
}

/** What a peek shows of a session: the text being written now, else its last message from the model, else its last
 * summary. */
export function lastWords(items: Item[], delta: string | undefined, summaries: Summary[]): string {
  items = hideReplyLines(items, summaries);
  if (delta?.trim()) return delta;
  for (let k = items.length - 1; k >= 0; k--) {
    const it = items[k]!;
    if (it.kind === "message" && it.data.role === "assistant" && it.data.text.trim()) return it.data.text;
  }
  return summaries.at(-1)?.text ?? "";
}

/** The number in the title and the app badge: the cards waiting for the owner. */
export function waitingCount(sessions: Session[]): number {
  return sessions.reduce((n, s) => n + (s.waiting > 0 ? s.waiting : s.state === "waiting" ? 1 : 0), 0);
}
