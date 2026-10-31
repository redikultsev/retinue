// A mock of the platform's face API (docs/face.md «API») for developing the face without the stack: login by
// code or Telegram init data, the snapshots, the event stream with `delta`, decisions, the diff, and the 410 path.
// Everything lives in memory; restart to start over. Run: `pnpm mock` (port 9601), or `pnpm dev` with Vite.
//
// Mock-only endpoints (not in the contract), for trying the edges by hand:
//   POST /api/_mock/approval {session?}  a new shown approval card arrives (an `approval` event)
//   POST /api/_mock/prune                old events are pruned and streams cut: clients get 410 and resync
import { randomBytes, randomUUID } from "node:crypto";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import * as fx from "./fixtures.ts";
import { mockSpaces } from "./spaces.ts";
import type { Approval, Input, Item, Session, Summary } from "../src/lib/types.ts";

const PORT = Number(process.env.MOCK_PORT ?? 9601);
const CODE = process.env.MOCK_CODE ?? "RTNE7K2QXP";
const COOKIE = "retinue_mock"; // the real one is __Host-retinue (Secure): this mock runs over plain http
const ORIGINS = new Set(["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:4173", "http://localhost:4173"]);
const now = () => Date.now() / 1000;

// --- «Waiting for you» (retinue/feed.py): the shown cards, the idle sessions' last summaries, a fixed digest ----------

/** «From the assistant»: a person's rows, each with its token `t`; a press hides one until «Undo» (`open`). */
const ASKS = [
  { key: "assistant:chat:1", source: "chat", sphere: "travel", sphere_title: "travel", title: "Anna Petrova", since: now() - 6 * 3600, essence: "reply: is an airport transfer needed", ref: "chat:1", t: "a".repeat(32) },
  { key: "assistant:letter:2", source: "letter", sphere: "home", sphere_title: "home", title: "Acme Hosting", since: now() - 2 * 86400, essence: "pay: the hosting invoice for May · deadline Friday (+2)", ref: "letter:2", t: "b".repeat(32) },
  { key: "assistant:chat:3", source: "chat", sphere: null, sphere_title: null, title: "Ivan Example", since: now() - 9 * 86400, essence: "decide: which of the two venues to book for the team dinner, the one by the river or the one near the office, before both stop holding the date", ref: "chat:3", t: "c".repeat(32) },
  { key: "assistant:letter:4", source: "letter", sphere: null, sphere_title: null, title: "Example Bank", since: now() - 4 * 86400, essence: "decide: confirm a sign-in from a new device · deadline today", ref: "letter:4", t: "d".repeat(32) },
  { key: "assistant:letter:5", source: "letter", sphere: "home", sphere_title: "home", title: "Example Studio", since: now() - 86400, essence: "reply: a second call next week · deadline Monday", ref: "letter:5", t: "e".repeat(32) },
];
/** The others of hers: a draft waiting for «Send». */
const OTHERS = [
  { key: "assistant:draft:0000000c", source: "draft", sphere: null, sphere_title: null, title: "Anna Petrova", since: now() - 1800, essence: "a reply draft waits for «Send»", ref: "draft:0000000c" },
];
const GMAIL = (thread: string) => ({ url: `https://mail.google.com/mail/?authuser=me@example.com#all/${thread}`, label: "Open in Gmail" });
/** A row's sheet (`/api/waiting/detail`), as the router would say it: a moment later, invented. Ivan's router is
 * silent (504 after a short wait), Example Bank's row is out of date (410). */
const DETAILS: Record<string, unknown> = {
  "chat:1": { kind: "chat", who: "Anna Petrova", account: "", more: 0, open: { url: "https://t.me/anna_example", label: "Open the chat" },
    asks: [{ need: "reply", text: "Anna asks whether you need a transfer from the airport on Saturday, and if so for how many people.", deadline: "Thursday", at: now() - 6 * 3600 }] },
  "letter:2": { kind: "letter", who: "Acme Hosting", account: "me@example.com", more: 0, open: GMAIL("18c2f0a1b2c3d4e7"),
    asks: [
      { need: "pay", text: "The last reminder before suspension.", deadline: "", at: now() - 3600, open: GMAIL("18c2f0a1b2c3d4e7") },
      { need: "pay", text: "A reminder of the same invoice.", deadline: "Friday", at: now() - 86400, open: GMAIL("18c2f0a1b2c3d4e6") },
      { need: "pay", text: "The invoice for May: ‹number›. Pay by card in the account's billing page.\nThe server is suspended if it is not paid by Friday.", deadline: "Friday", at: now() - 2 * 86400, open: GMAIL("18c2f0a1b2c3d4e5") },
    ] },
  "letter:5": { kind: "letter", who: "Example Studio", account: "me@example.com", more: 0, open: GMAIL("18c2f0a1b2c3d4f1"),
    asks: [
      { need: "reply", text: "They invite you to a second call next week and ask which two slots suit you.", deadline: "Monday", at: now() - 86400, open: GMAIL("18c2f0a1b2c3d4f1") },
    ] },
  "draft:0000000c": { kind: "draft", who: "Anna Petrova", account: "", more: 0, open: { url: "https://t.me/anna_example", label: "Open the chat" },
    asks: [{ need: "reply", text: "Anna asks about the flight time.", deadline: "", at: now() - 7200 }],
    draft: { text: "Hi Anna!\n\nThe flight lands at ‹number›, so pick-up at the arrivals hall works.\n\nSee you soon", channel: "telegram", subject: "", expires: now() + 4 * 3600, state: "shown" } },
};
const pressed = new Set<string>();
let closedToday = 1;

function mockFeed() {
  const row = (s: Session, over: Record<string, unknown>) => ({
    session: s.id, track: s.track, sphere: null, sphere_title: null, sphere_icon: null, title: s.title, ref: s.id, ...over,
  });
  const cards = [...approvals.values()].filter((a) => (a.kind === "approval" || a.kind === "question") && a.state === "shown");
  const answer = cards.filter((a) => a.kind === "question" && sessions.get(a.session));
  const decide = cards.filter((a) => a.kind === "approval" && sessions.get(a.session));
  const read = [...sessions.values()].filter((s) => s.state === "idle");
  const last = (id: string) => [...summaries.values()].filter((x) => x.session === id).at(-1);
  const groups = [
    { action: "answer", title: "Answer a question", rows: answer.map((a) => row(sessions.get(a.session)!, { key: `card:${a.id}`, action: "answer", source: "question", since: a.created, essence: "", approval: a })) },
    { action: "decide", title: "Decide", rows: decide.map((a) => row(sessions.get(a.session)!, { key: `card:${a.id}`, action: "decide", source: "approval", since: a.created, essence: "", approval: a })) },
    { action: "read", title: "Read", rows: read.map((s) => row(s, { key: `read:${s.id}`, action: "read", source: "session", since: s.at, essence: last(s.id)?.text.split("\n")[0] ?? "" })) },
    { action: "assistant", title: "From the assistant", rows: [
      ...ASKS.filter((a) => !pressed.has(a.t)).map((a) => ({ action: "assistant", session: null, track: null, sphere_icon: null, ...a })),
      ...OTHERS.map((a) => ({ action: "assistant", session: null, track: null, sphere_icon: null, ...a })),
      { key: "assistant:deadline:1", action: "assistant", source: "deadline", session: null, track: null, sphere: "home", sphere_title: "home", sphere_icon: null, title: "Base", since: now() - 86400, essence: "tomorrow: Pay for the internet — due", ref: "deadline:1" },
    ] },
  ].filter((g) => g.rows.length);
  const day = (n: number) => new Date(Date.now() + n * 86400_000).toISOString().slice(0, 10);
  const deadlines = [
    { id: "deadline:1", when: day(1), title: "tomorrow: Pay for the internet — due", sphere: "home", sphere_title: "Home", sphere_icon: "🏠", path: "home/journal/2026-05-01-internet.md" },
    { id: "deadline:2", when: day(5), title: "in 5 days: Gym membership — valid until", sphere: "home", sphere_title: "Home", sphere_icon: "🏠", path: "home/profile/gym.md" },
  ];
  const chips = [
    { key: "health", text: "all working", level: "ok" },
    { key: "backup", text: "backup 3 h ago", level: "ok" },
    { key: "limit", text: "limit 74 %", level: "warn" },
    { key: "build", text: "pages built", level: "ok" },
  ];
  return { schema: 1, count: groups.reduce((n, g) => n + g.rows.length, 0), groups, bot: "retinue_mock_bot", assistant_at: now() - 600, deadlines, chips, closed_today: closedToday, expired_week: 2 };
}

// --- state and the event log -----------------------------------------------------------------------------------

const sessions = new Map<string, Session>(fx.sessions.map((s) => [s.id, { ...s }]));
const items = new Map<string, Item>(fx.items().map((it) => [`${it.session}/${it.turn}/${it.n}`, it]));
const approvals = new Map<string, Approval>(fx.approvals.map((a) => [a.id, { ...a }]));
// keyed `<session>/<note seq>`, as the platform keys summary events
const baseCards = new Map(fx.baseCards.map((c) => [c.id, c]));
const summaries = new Map<string, Summary>(fx.summaries.map((s) => [`${s.session}/${s.seq}`, s]));
let noteSeq = 100;
const MOCK_PAGES = "https://pages.in.example.test";
const library = new Map(
  [
    { id: "regex-course", title: "Regex in an evening", section: "education", section_title: "Study", section_icon: "🎓", minutes: 95, updated: "2026-10-27" },
    { id: "sql-cheatsheet", title: "SQL: cheat sheet", section: "education", section_title: "Study", section_icon: "🎓", minutes: 40, updated: "2026-10-20" },
    { id: "ensk-notes", title: "Ensk: notes for the trip", section: "travel", section_title: "Travel", section_icon: "✈️", minutes: 6, updated: "2026-10-22" },
    { id: "loose-page", title: "A page with no section", section: null, section_title: null, section_icon: null, minutes: null, updated: "2026-09-30" },
  ].map((p) => [p.id, { ...p, url: `${MOCK_PAGES}/${p.id}`, opened: null as number | null, done: null as number | null }]),
);
const cookies = new Set<string>();
const seenInputs = new Map<string, unknown>();
// keyed `<session>/<input_id>`; attachments by name only (no thumbnails here)
const inputs = new Map<string, Input>(fx.inputs.map((x) => [`${x.session}/${x.input_id}`, x]));
let failures: number[] = [];
let lockedUntil = 0;

interface Event {
  id: number;
  kind: string;
  key: string;
  data: unknown;
}
const events: Event[] = [];
let lastId = 100; // a fresh client starts at the list's cursor
let oldestKept = 0; // a stream asked from a cursor below it gets 410

const streams = new Set<ServerResponse>();

function send(res: ServerResponse, kind: string, data: unknown, id?: number) {
  res.write(`${id !== undefined ? `id: ${id}\n` : ""}event: ${kind}\ndata: ${JSON.stringify(data)}\n\n`);
}

function emit(kind: string, key: string, data: unknown) {
  const event = { id: ++lastId, kind, key, data };
  events.push(event);
  for (const res of streams) send(res, kind, data, event.id);
}

function delta(session: string, text: string) {
  for (const res of streams) send(res, "delta", { session, text });
}

function setSession(id: string, patch: Partial<Session>) {
  const s = sessions.get(id);
  if (!s) return;
  const next = { ...s, ...patch, at: now() };
  if (next.state === "active") delete next.archived; // a turn begins: back in the list
  next.waiting = [...approvals.values()].filter((a) => a.session === id && a.kind === "approval" && a.state === "shown").length;
  sessions.set(id, next);
  emit("session", id, next);
}

function putItem(session: string, turn: number, n: number, item: Omit<Item, "session" | "turn" | "n">) {
  const full = { ...item, session, turn, n } as Item;
  items.set(`${session}/${turn}/${n}`, full);
  emit("item", `${session}/${turn}/${n}`, full);
}

function putApproval(a: Approval) {
  approvals.set(a.id, a);
  emit("approval", a.id, a);
}

function nextTurn(session: string): number {
  let max = -1;
  for (const it of items.values()) if (it.session === session) max = Math.max(max, it.turn);
  return max + 1;
}

function itemsOf(session: string): Item[] {
  return [...items.values()].filter((i) => i.session === session).sort((a, b) => a.turn - b.turn || a.n - b.n);
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// --- simulated turns ---------------------------------------------------------------------------------------------

/** The owner wrote: the session runs a short turn — a delta streamed word by word, a tool call, the answer. */
async function simulateTurn(session: string, text: string) {
  const turn = nextTurn(session);
  setSession(session, { state: "active" });
  // The owner's words are the `input` event the /input route emitted, as on the platform: no user item here.
  await sleep(600);
  const comment = /^Comment on (.+?):(\d+): /.exec(text);
  const reply = comment
    ? `Got the note on \`${comment[1]}\`, line ${comment[2]}. Fixing it and running the tests.`
    : `Got it: “${text.slice(0, 60)}”. Looking at what can be done; back with a result.`;
  for (const word of reply.split(/(?<= )/)) {
    delta(session, word);
    await sleep(90);
  }
  putItem(session, turn, 1, { kind: "message", data: { role: "assistant", text: reply } });
  putItem(session, turn, 2, { kind: "tool", data: { id: `toolu_${randomUUID().slice(0, 8)}`, tool: "Bash", input: { command: "git status --short" }, result: null, error: false } });
  await sleep(700);
  putItem(session, turn, 2, { kind: "tool", data: { id: "toolu_status", tool: "Bash", input: { command: "git status --short" }, result: " M retinue/gate.py\n M tests/test_gate.py\n?? docs/face.md", error: false } });
  const final = "Done: the fix is in, the tests are green.";
  for (const word of final.split(/(?<= )/)) {
    delta(session, word);
    await sleep(90);
  }
  putItem(session, turn, 3, { kind: "message", data: { role: "assistant", text: final } });
  putItem(session, turn, 4, { kind: "result", data: { stop_reason: "end_turn", is_error: false, text: final, deferred: null } });
  const s = sessions.get(session)!;
  const summary = { session, seq: ++noteSeq, head: `Result · ${s.track} · session ${session}`, text: final, at: now() };
  summaries.set(`${session}/${summary.seq}`, summary);
  emit("summary", `${session}/${summary.seq}`, summary);
  setSession(session, { state: s.kind === "code" ? "closed" : "idle" });
}

/** The always-running session: a delta streamed in a loop, finalised into an item every round (E8 demo). */
async function streamForever(session: string) {
  for (let round = 0; ; round++) {
    const turn = nextTurn(session);
    if (sessions.get(session)?.state !== "active") setSession(session, { state: "active" });
    for (const chunk of fx.STREAM_TEXT) {
      delta(session, chunk);
      await sleep(350);
    }
    putItem(session, turn, 0, { kind: "message", data: { role: "assistant", text: fx.STREAM_TEXT.join("") } });
    putItem(session, turn, 1, { kind: "reads", data: { calls: [{ id: `toolu_rd${round}`, tool: "Grep", input: { pattern: "\\]\\(", path: "/work/kb" }, result: "work/AGENTS.md:12\ntravel/AGENTS.md:40", error: false }] } });
    await sleep(4000);
  }
}

// --- http --------------------------------------------------------------------------------------------------------

function json(res: ServerResponse, status: number, data: unknown, headers: Record<string, string> = {}) {
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", ...headers });
  res.end(JSON.stringify(data));
}

async function body(req: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  for await (const c of req) chunks.push(c as Buffer);
  try {
    const parsed = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function authed(req: IncomingMessage): boolean {
  const cookie = req.headers.cookie ?? "";
  const m = new RegExp(`(?:^|; )${COOKIE}=([^;]+)`).exec(cookie);
  return !!m && cookies.has(m[1]!);
}

function login(res: ServerResponse) {
  const token = randomBytes(32).toString("hex");
  cookies.add(token);
  json(res, 200, { ok: true }, { "Set-Cookie": `${COOKIE}=${token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000` });
}

function failed(res: ServerResponse, message: string) {
  const t = now();
  failures = [...failures.filter((f) => t - f < 600), t];
  if (failures.length >= 10) lockedUntil = t + 600;
  json(res, 401, { error: message });
}

function sessionSnapshot(id: string) {
  const session = sessions.get(id)!;
  return {
    schema: 1,
    cursor: lastId,
    session,
    items: itemsOf(id),
    approvals: [...approvals.values()].filter((a) => a.session === id && a.state !== "pending"),
    summaries: [...summaries.values()].filter((x) => x.session === id).sort((a, b) => a.at - b.at),
    inputs: [...inputs.values()].filter((x) => x.session === id).sort((a, b) => a.at - b.at),
    files: fx.files.filter((f) => f.file.session === id).map((f) => f.file),
    push_cards: fx.pushCards.filter((p) => p.session === id),
    base_cards: [...baseCards.values()].filter((b) => b.session === id),
    about: fx.about(session),
  };
}

async function route(req: IncomingMessage, res: ServerResponse) {
  const url = new URL(req.url ?? "/", "http://mock");
  const path = url.pathname;
  const method = req.method ?? "GET";

  if (method === "POST") {
    if (req.headers["x-retinue"] !== "1") return json(res, 403, { error: "No X-Retinue header." });
    const origin = req.headers.origin;
    if (origin && !ORIGINS.has(origin)) return json(res, 403, { error: "Foreign Origin." });
  }

  // auth
  if (path.startsWith("/api/auth/")) {
    if (now() < lockedUntil) return json(res, 429, { error: "Too many attempts — wait 10 minutes." });
    const b = method === "POST" ? await body(req) : {};
    if (path === "/api/auth/code" && method === "POST") {
      return String(b.code ?? "").toUpperCase() === CODE ? login(res) : failed(res, "The code did not work.");
    }
    if (path === "/api/auth/telegram" && method === "POST") {
      // The real platform checks the HMAC, auth_date within 5 minutes and user.id; the mock only the shape.
      const data = new URLSearchParams(String(b.init_data ?? ""));
      return data.get("hash") && data.get("user") ? login(res) : failed(res, "Telegram did not confirm the login.");
    }
    if (path === "/api/auth/logout" && method === "POST") {
      return json(res, 200, { ok: true }, { "Set-Cookie": `${COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0` });
    }
    return json(res, 404, { error: "Not found." });
  }
  if (!authed(req)) return json(res, 401, { error: "Log in first." });

  if (path === "/api/meta") return json(res, 200, { schema: 1, owner_ok: true, tracks: fx.tracks, general: "notes", spheres: fx.spheres, face_origin: "http://127.0.0.1:5173", speech: true });

  // The owner's voice: the recording is read and dropped; a canned transcript per mode (no ElevenLabs here).
  if (path === "/api/speech" && method === "POST") {
    let size = 0;
    for await (const chunk of req) size += (chunk as Buffer).length;
    if (!size) return json(res, 400, { error: "The recording is empty." });
    if (url.searchParams.get("mode") === "en") {
      const text = "So, um, I think the KV cache, uh, is the bottleneck";
      return json(res, 200, { text, message: `(by voice, verbatim) ${text}` });
    }
    return json(res, 200, { text: "check the KV cache and make an eval for RAG" });
  }

  if (path === "/api/feed") return json(res, 200, mockFeed());
  if (path === "/api/stats") return json(res, 200, mockStats());
  if (path === "/api/waiting/press" && method === "POST") {
    const { t, action } = (await body(req)) as { t?: string; action?: string };
    if (typeof t !== "string" || !/^[0-9a-f]{32}$/.test(t) || !["done", "dismissed", "later", "open"].includes(action ?? ""))
      return json(res, 400, { error: "Need t — the row's token — and action: done, dismissed, later or open." });
    if (action === "open") {
      if (pressed.delete(t)) closedToday -= 1;
    } else if (!pressed.has(t)) {
      pressed.add(t);
      closedToday += 1;
    }
    return json(res, 200, { ok: true });
  }

  if (path === "/api/waiting/detail" && method === "POST") {
    const { ref, t } = (await body(req)) as { ref?: string; t?: string };
    const row = [...ASKS, ...OTHERS].find((r) => r.ref === ref);
    if (typeof ref !== "string") return json(res, 400, { error: "Need ref — the row's id — and t, its token, on a person's row." });
    if (!row || ("t" in row && row.t !== t) || ref === "letter:4")
      return json(res, 410, { error: "The assistant no longer has this row: the list is out of date." });
    if (ref === "chat:3") {
      await new Promise((done) => setTimeout(done, 3000));
      return json(res, 504, { error: "The assistant's side did not answer in time." });
    }
    await new Promise((done) => setTimeout(done, 700));
    return json(res, 200, { detail: DETAILS[ref] });
  }

  const space = method === "GET" ? mockSpaces(path) : null;
  if (space) return json(res, space.status, space.data);
  // «Pages»: synthetic pages on a made-up pages service; `open` and `done` kept in memory like the platform's table.
  if (path === "/api/library" && method === "GET") return json(res, 200, { schema: 1, origin: MOCK_PAGES, at: now() - 120, pages: [...library.values()] });
  const reading = /^\/api\/library\/([a-z0-9-]+)\/(open|done)$/.exec(path);
  if (reading && method === "POST") {
    const page = library.get(reading[1] ?? "");
    if (!page) return json(res, 404, { error: "There is no such page in the library." });
    const b = await body(req);
    if (reading[2] === "open") page.opened = now();
    else if (typeof b.done === "boolean") page.done = b.done ? now() : null;
    else return json(res, 400, { error: "done: true or false." });
    return json(res, 200, { page });
  }

  if (path === "/api/sessions" && method === "GET") return json(res, 200, { schema: 1, cursor: lastId, sessions: [...sessions.values()] });

  if (path === "/api/sessions" && method === "POST") {
    const b = await body(req);
    const key = String(b.input_id ?? "");
    if (seenInputs.has(key)) return json(res, 200, seenInputs.get(key));
    const sphere = fx.spheres.find((x) => x.name === b.sphere);
    const track = fx.tracks.find((t) => t.name === (b.track ?? sphere?.track));
    if (!track || !String(b.text ?? "").trim()) return json(res, 400, { error: "A track and a text are needed." });
    const id = randomBytes(4).toString("hex");
    const text = String(b.text);
    sessions.set(id, { id, track: track.name, lane: track.lane, kind: track.kind, state: "active", created: now(), at: now(), title: text.slice(0, 80), waiting: 0, sphere: sphere?.name ?? null });
    emit("session", id, sessions.get(id));
    const out = { session: id };
    seenInputs.set(key, out);
    void simulateTurn(id, text);
    return json(res, 200, out);
  }

  if (path === "/api/events") {
    const header = req.headers["last-event-id"];
    const since = Number(header ?? url.searchParams.get("since") ?? 0);
    if (since < oldestKept) return json(res, 410, { error: "The cursor is out of date — reload the snapshot." });
    res.writeHead(200, { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-store", Connection: "keep-alive", "X-Accel-Buffering": "no" });
    res.write("retry: 2000\n\n");
    for (const e of events) if (e.id > since) send(res, e.kind, e.data, e.id);
    streams.add(res);
    const ping = setInterval(() => res.write(": ping\n\n"), 15_000);
    req.on("close", () => {
      clearInterval(ping);
      streams.delete(res);
    });
    return;
  }

  const b = /^\/api\/base\/([0-9a-f]{8})(\/[a-z]+)?$/.exec(path);
  if (b) {
    const card = baseCards.get(b[1]!);
    if (!card) return json(res, 404, { error: "No such card." });
    if (!b[2] && method === "GET") return json(res, 200, { card });
    if (b[2] === "/diff" && method === "GET") return json(res, 200, { diff: fx.BASE_DIFF, truncated: false });
    if (b[2] === "/undo" && method === "POST") {
      if (!card.undo) return json(res, 200, { toast: "Already undone.", card });
      const done = { ...card, state: "undone" as const, undo: false, undone: "9e8d7c6b5a41", at: now(),
        said: `Undone: 9e8d7c6 puts back ${card.files.length} files.` };
      baseCards.set(card.id, done);
      emit("base", card.id, done);
      return json(res, 200, { toast: done.said, card: done });
    }
    return json(res, 405, { error: "Not here." });
  }

  let m = /^\/api\/sessions\/([0-9a-f]{8})(\/[a-z]+)?$/.exec(path);
  if (m) {
    const id = m[1]!;
    const sub = m[2];
    const s = sessions.get(id);
    if (!s) return json(res, 404, { error: "No such session." });
    if (!sub && method === "GET") return json(res, 200, sessionSnapshot(id));
    if (sub === "/diff" && method === "GET") {
      if (s.kind !== "code") return json(res, 400, { error: "This session has no code." });
      return json(res, 200, { cwd: `/work/${s.track}`, stat: fx.STAT, diff: fx.DIFF, truncated: false });
    }
    if (sub === "/input" && method === "POST") {
      const b = await body(req);
      const key = String(b.input_id ?? "");
      if (seenInputs.has(key)) return json(res, 200, seenInputs.get(key));
      const text = String(b.text ?? "").trim();
      const attached = Array.isArray(b.attachments) ? (b.attachments as { name?: string; data?: string }[]) : [];
      if (!text && !attached.length) return json(res, 400, { error: "Empty message." });
      const turn = nextTurn(id);
      const input: Input = {
        session: id, input_id: key, text, at: now(), turn, n: 0,
        attachments: attached.map((f) => ({ kind: "file", name: String(f.name ?? ""), size: Math.floor((f.data?.length ?? 0) * 3 / 4), what: `file “${f.name}”`, thumb: false })),
      };
      inputs.set(`${id}/${key}`, input);
      emit("input", `${id}/${key}`, input);
      // A message while a card is open makes the card stale, as in Telegram.
      for (const a of approvals.values()) if (a.session === id && a.state === "shown") putApproval({ ...a, state: "stale", at: now(), view: { ...a.view, status: "Expired: you wrote a new message" } });
      const out = { say: "Accepted." };
      seenInputs.set(key, out);
      void simulateTurn(id, text);
      return json(res, 200, out);
    }
    if (sub === "/stop" && method === "POST") {
      setSession(id, { state: "idle" });
      return json(res, 200, { ok: true });
    }
    if (sub === "/archive" && method === "POST") {
      for (const a of approvals.values()) if (a.session === id && a.state === "shown") approvals.set(a.id, { ...a, state: "stale" });
      setSession(id, { state: "closed", archived: true, waiting: 0 });
      return json(res, 200, { session: sessions.get(id) });
    }
    if (sub === "/pin" && method === "POST") {
      const b = await body(req);
      if (typeof b.pinned !== "boolean") return json(res, 400, { error: "pinned: true | false" });
      sessions.set(id, { ...s, pinned: b.pinned });
      emit("session", id, sessions.get(id));
      return json(res, 200, { session: sessions.get(id) });
    }
    if ((sub === "/sphere" || sub === "/main" || sub === "/seen") && method === "POST") {
      // The platform's rules in short: a new sphere drops the main mark; one main session per sphere.
      const b = await body(req);
      if (sub === "/sphere") sessions.set(id, { ...s, sphere: (b.sphere as string | null) ?? null, main: false });
      if (sub === "/main") {
        if (!s.sphere) return json(res, 400, { error: "Choose the session's sphere first." });
        for (const o of sessions.values()) if (b.main && o.id !== id && o.sphere === s.sphere && o.main) setSession(o.id, { main: false });
        sessions.set(id, { ...s, main: !!b.main });
      }
      if (sub === "/seen") sessions.set(id, { ...s, unread: false });
      emit("session", id, sessions.get(id));
      return json(res, 200, { session: sessions.get(id) });
    }
    if (sub === "/fork" && method === "POST") {
      const b = await body(req);
      const text = String(b.text ?? "").trim();
      if (!text) return json(res, 400, { error: "Empty message." });
      const fork = randomBytes(4).toString("hex");
      sessions.set(fork, { ...s, id: fork, state: "active", created: now(), at: now(), title: text.slice(0, 80), waiting: 0, pinned: false, main: false, fork_of: id });
      emit("session", fork, sessions.get(fork));
      void simulateTurn(fork, text);
      return json(res, 200, { session: fork, say: `Session ${fork} · ${s.track}.` });
    }
    if (sub === "/side" && method === "POST") {
      // The real platform starts the side session with an excerpt of this one's last items; the mock only the shape.
      const b = await body(req);
      const key = String(b.input_id ?? "");
      if (seenInputs.has(key)) return json(res, 200, seenInputs.get(key));
      const text = String(b.text ?? "").trim();
      if (!text) return json(res, 400, { error: "Empty question." });
      const side = randomBytes(4).toString("hex");
      sessions.set(side, { ...s, id: side, state: "active", created: now(), at: now(), title: text.slice(0, 80), waiting: 0, pinned: false, parent: s.parent ?? id });
      emit("session", side, sessions.get(side));
      const out = { session: side, say: `Session ${side} · ${s.track}.` };
      seenInputs.set(key, out);
      void simulateTurn(side, text);
      return json(res, 200, out);
    }
  }

  m = /^\/api\/approvals\/([0-9a-f]{8})$/.exec(path);
  if (m && method === "GET") {
    const a = approvals.get(m[1]!);
    return a && a.state !== "pending" ? json(res, 200, { approval: a }) : json(res, 404, { error: "No such card." });
  }

  m = /^\/api\/approvals\/([0-9a-f]{8})\/done$/.exec(path);
  if (m && method === "POST") {
    const a = approvals.get(m[1]!);
    if (!a) return json(res, 404, { error: "No such card." });
    if (a.kind !== "courier") return json(res, 400, { error: "Only a courier card is closed this way." });
    if (a.state !== "dropped") return json(res, 200, { toast: "Already closed.", approval: a });
    const closed: Approval = { ...a, state: "done", at: now() };
    putApproval(closed);
    return json(res, 200, { toast: "Closed.", approval: closed });
  }

  m = /^\/api\/approvals\/([0-9a-f]{8})\/decide$/.exec(path);
  if (m && method === "POST") {
    const a = approvals.get(m[1]!);
    const b = await body(req);
    if (!a) return json(res, 404, { error: "No such card." });
    if (a.kind !== "approval" || a.state !== "shown" || b.digest !== a.digest || (b.action !== "approve" && b.action !== "reject")) {
      return json(res, 200, { toast: "The card is stale — the decision was not taken.", approval: a });
    }
    const granted = b.action === "approve";
    const decided: Approval = { ...a, state: granted ? "granted" : "rejected", at: now(), view: { ...a.view, status: granted ? "Allowed — running it." : "Denied." } };
    putApproval(decided);
    setSession(a.session!, { state: "active" });
    void (async () => {
      await sleep(800);
      const s = a.session!;
      const turn = nextTurn(s);
      if (granted) {
        putApproval({ ...decided, state: "executing", at: now() });
        await sleep(800);
        putApproval({ ...decided, state: "executed", at: now() });
      }
      const text = granted ? "The call ran: 13 passed. After a restart the card is pressed once." : "Understood, not running it. What should I do instead?";
      for (const w of text.split(/(?<= )/)) {
        delta(s, w);
        await sleep(80);
      }
      putItem(s, turn, 0, { kind: "message", data: { role: "assistant", text } });
      putItem(s, turn, 1, { kind: "result", data: { stop_reason: "end_turn", is_error: false, text, deferred: null } });
      setSession(s, { state: sessions.get(s)!.kind === "code" ? "closed" : "idle" });
    })();
    return json(res, 200, { toast: granted ? "Allowed." : "Denied.", approval: decided });
  }

  // A handed-over file, with the headers the platform sends (retinue/face.py `file`, `view`).
  const f = /^\/api\/files\/([^/]+)(\/view)?$/.exec(path);
  if (f && method === "GET") {
    const found = fx.files.find((x) => x.file.id === f[1]);
    if (!found || (f[2] && !found.file.mime.startsWith("text/html"))) return json(res, 404, { error: "No such file." });
    res.writeHead(200, {
      "Content-Type": found.file.mime,
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      "Content-Security-Policy": f[2]
        ? "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'; sandbox"
        : "default-src 'none'; sandbox",
      ...(f[2] ? {} : { "Content-Disposition": `attachment; filename*=UTF-8''${encodeURIComponent(found.file.name)}` }),
    });
    return res.end(found.body);
  }

  if (path === "/api/_mock/approval" && method === "POST") {
    const b = await body(req);
    const session = String(b.session ?? "0a1b2c3d");
    const s = sessions.get(session);
    if (!s) return json(res, 404, { error: "No such session." });
    const command = "rm -rf /work/notes/.cache && du -sh /work/notes";
    const id = randomBytes(4).toString("hex");
    putApproval({
      id, session, tool_use_id: `toolu_${id}`, kind: "approval", state: "shown", tool: "Bash", input: { command }, cwd: `/work/${s.track}`,
      digest: randomBytes(32).toString("hex"),
      view: { head: [`Approval · ${s.track} · session ${session}`, "Tool: Bash", `Folder: /work/${s.track}`, `Input, ${command.length} characters:`], body: command },
      created: now(), at: now(),
    });
    putItem(session, nextTurn(session), 0, { kind: "tool", data: { id: `toolu_${id}`, tool: "Bash", input: { command }, result: null, error: false } });
    setSession(session, { state: "waiting" });
    return json(res, 200, { approval: id });
  }

  if (path === "/api/_mock/prune" && method === "POST") {
    // As if a week of events had gone by and been pruned: every cursor a client holds is now too old.
    lastId += 1000;
    oldestKept = lastId;
    events.length = 0;
    for (const s of streams) s.end(); // the browser reconnects with Last-Event-ID and gets 410
    streams.clear();
    return json(res, 200, { oldest_kept: oldestKept });
  }

  return json(res, 404, { error: "Not found." });
}

createServer((req, res) => {
  route(req, res).catch((e) => {
    console.error(e);
    if (!res.headersSent) json(res, 500, { error: "Mock error." });
  });
}).listen(PORT, "127.0.0.1", () => {
  console.log(`mock face API on http://127.0.0.1:${PORT} — login code: ${CODE}`);
  void streamForever("5566aabb");
});

/** «Week»: eight made-up seven-day spans, the newest first. */
function mockStats() {
  const now = Date.now() / 1000;
  const week = 7 * 86400;
  const weeks = [12, 9, 14, 7, 10, 6, 8, 5].map((n, i) => ({
    since: now - (i + 1) * week,
    until: now - i * week,
    sessions: n,
    sub_sessions: i % 3,
    runs: n * 3 + i,
    owner: { messages: n * 4, stops: i % 2 },
    engine: { tool_errors: (n * 7) % 5, tool_errors_by_tool: { Bash: (n * 7) % 5 }, compactions: i % 2, run_errors: i === 0 ? 1 : 0 },
    gate: {
      calls: n * 40,
      by_decision: { rule: n * 30, classifier: n * 8, blocked: 1, defer: n % 4, deny: 1 },
      reasons: { "defer: Waiting for the owner's decision": n % 4, "deny: The owner refused": 1 },
    },
    cards: {
      approval: { shown: n % 5, granted: (n % 5) - 1 > 0 ? (n % 5) - 1 : 0, answered: 0, rejected: n % 5 ? 1 : 0, open: 0, lapsed: 0 },
      question: { shown: 1, granted: 0, answered: 1, rejected: 0, open: 0, lapsed: 0 },
      courier: i % 2,
    },
  }));
  return { schema: 1, now, weeks };
}
