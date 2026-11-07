// A mock of the platform's face API (docs/face.md «API») for developing the face without the stack: login by
// code or Telegram init data, the snapshots, the event stream with `delta`, decisions, the diff, and the 410 path.
// Everything lives in memory; restart to start over. Run: `pnpm mock` (port 9601), or `pnpm dev` with Vite.
//
// Mock-only endpoints (not in the contract), for trying the edges by hand:
//   POST /api/_mock/approval {session?}  a new shown approval card arrives (an `approval` event)
//   POST /api/_mock/prune                old events are pruned and streams cut: clients get 410 and resync
//   POST /api/_mock/collection[?quiet=1] a deal of the «work» Space moves a column on (a `collection` event; quiet:
//                                        none, so the app's next write of that deal meets a conflict)
//   POST /api/_mock/assistant            the assistant's plan moves the report's block (lit on the grid, with «Undo»)
import { createHash, randomBytes, randomUUID } from "node:crypto";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { type Feed, type FeedRow, paths, type TurnEnding, type WaitingDetail } from "../src/lib/contract.ts";
import type { Approval, Input, Item, Proposal, Session, Summary } from "../src/lib/types.ts";
import { type Moved, mockCollections, mockProposals, mockWrite, moveDeal, seedProposals, waitingProposals } from "./collections.ts";
import { mockChat } from "./chat.ts";
import { closed, digest, pressRow } from "./digest.ts";
import * as fx from "./fixtures.ts";
import { cleanPress, mockAssistantMove, mockPlan, mockPlanPress } from "./plan.ts";
import { settingsPost, settingsView } from "./settings.ts";
import { mockBase, mockSpaces } from "./spaces.ts";

const PORT = Number(process.env.MOCK_PORT ?? 9601);
const CODE = process.env.MOCK_CODE ?? "RTNE7K2QXP";
const COOKIE = "retinue_mock"; // the real one is __Host-retinue (Secure): this mock runs over plain http
const ORIGINS = new Set(["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:4173", "http://localhost:4173"]);
const now = () => Date.now() / 1000;
// what a question's session wrote just before asking (the feed's `said`)
const SAID =
  "Since April the €12 plan has added three talks on the books you picked, and a month is enough to read what matters.\n\n**Recommend:** one month on the €12 plan, cancel the renewal right after paying\n**Why:** three new talks on the May list; the rest repeats the free part\n**Cost:** €12 and about four evenings\n**Not checked:** whether old posts stay open to new members";

// --- «Waiting for you» (retinue/feed.py): the shown cards, the idle sessions' last summaries, the digest --------------

const GMAIL = (thread: string) => ({ url: `https://mail.google.com/mail/?authuser=me@example.com#all/${thread}`, label: "Open in Gmail" });
/** A row's sheet (`/api/waiting/detail`), as the router would say it: a moment later, invented. Ivan's router is
 * silent (504 after a short wait), Example Bank's row is out of date (410). */
const DETAILS: Record<string, WaitingDetail> = {
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

/** A turn ending's one line, as the platform says it (retinue/feed.py `_ending_essence`). */
function endingLine(e: TurnEnding): string {
  const first = (e.artifact ?? (e.artifacts as unknown[] | undefined)?.[0] ?? {}) as Record<string, string | undefined>;
  const said = { ask: e.context ?? e.why, approve: e.will, look: first.title ?? first.path ?? first.url, step: e.what, done: e.what }[String(e.kind)];
  return String(said ?? e.request ?? "");
}

/** A made-up laptop's own sessions (retinue/computers.py), as it reports them with their turn endings: one at work, two
 * waiting on the owner (a question, a step of his), two back with a result, one elsewhere. `MOCK_LAPTOP_OFF=1`: the
 * laptop has not reported for a while. */
const LAPTOP_OFF = process.env.MOCK_LAPTOP_OFF === "1";
function laptop() {
  const link = (id: string) => `claude://claude.ai/epitaxy/${id}`;
  return [
    { id: "local_m1", title: "Compare the two stays", folder: "/home/anna/base/travel/lisbon", project: "travel/lisbon", state: "working", at: now() - 20, link: link("local_m1"), slot: 0 },
    { id: "local_m2", title: "Packing list", folder: "/home/anna/base/travel", project: "travel", state: "waiting", at: now() - 600, link: link("local_m2"), slot: 0,
      ending: { kind: "ask", request: "Make a packing list for Lisbon that fits in a carry-on", why_owner: "goal", context: "It fits in 7 kg with one pair of shoes; two pairs need a checked bag (€35).",
        options: [{ label: "One pair, carry-on only", effect: "no bag fee; walking shoes only" }, { label: "Check a bag", effect: "+€35, room for the dinner shoes" }], without: "I keep the carry-on list." } },
    { id: "local_m4", title: "Book the ferry", folder: "/home/anna/base/travel/lisbon", project: "travel/lisbon", state: "waiting", at: now() - 1800, link: link("local_m4"), slot: 1,
      ending: { kind: "step", what: "Pay for the two ferry seats — the site takes cards only from the passenger", where: "the ferry company's site, «My bookings»", command: "Booking ref: FX-1042", expect: "the confirmation number" } },
    { id: "local_m5", title: "The site's footer", folder: "/home/anna/code/acme-site", project: "work/acme/site", state: "done", at: now() - 3600, link: link("local_m5"), slot: 0,
      ending: { kind: "look", request: "Make the footer match the new header", artifacts: [{ title: "footer.png", src: null, kind: "other", why: "On your computer.", on_computer: true }, { url: "https://example.com/acme-preview", title: "The preview", src: "https://example.com/acme-preview", kind: "link" }], checks: ["the links line up with the header's"] } },
    { id: "local_m3", title: "Scratch notes", folder: "/tmp", project: "", state: "done", at: now() - 7200, slot: 0,
      ending: { kind: "done", request: "Tidy my scratch notes", what: "Merged 14 notes into 3 and dropped the duplicates.", next: "Nothing: they are filed.", next_by: "me" } },
  ];
}

/** The laptop's sessions as the feed's rows (retinue/feed.py `_computer_rows`): by their ending's kind, or «Reply». */
function laptopRows(): FeedRow[] {
  if (LAPTOP_OFF) return [];
  return laptop().flatMap((s): FeedRow[] => {
    const e = (s as { ending?: TurnEnding }).ending;
    if (s.state === "working" || (!e && s.state !== "waiting")) return [];
    const action = (e ? ({ ask: "answer", approve: "decide", step: "step", look: "read", done: "read" } as const)[e.kind as "ask"] : "reply") as FeedRow["action"];
    return [{
      key: `${e ? "ending" : "reply"}:laptop/${s.id}`, action, source: "computer", session: null, track: null, sphere: null, sphere_title: null, sphere_icon: null,
      project: s.project || null, title: s.title, since: s.at, essence: e ? endingLine(e) : "", ref: s.id,
      computer: { device: "laptop", id: s.id, ...(s.link ? { link: s.link } : {}) }, ...(e ? { ending: e } : {}),
    }];
  });
}

function mockFeed(): Feed {
  const row = (s: Session, over: Omit<FeedRow, "session" | "track" | "sphere" | "sphere_title" | "sphere_icon" | "title" | "ref" | "action"> & { action?: FeedRow["action"] }): FeedRow => ({
    session: s.id, track: s.track, sphere: null, sphere_title: null, sphere_icon: null, project: s.project ?? (s.sphere === "platform" ? "work/retinue" : (s.sphere ?? null)),
    title: s.title, ref: s.id, action: "read", ...over,
  });
  const cards = [...approvals.values()].filter((a) => (a.kind === "approval" || a.kind === "question") && a.state === "shown");
  const answer = cards.filter((a) => a.kind === "question" && sessions.get(a.session));
  const decide = cards.filter((a) => a.kind === "approval" && sessions.get(a.session));
  // a session whose turn ended with a word for the owner takes a row by its kind (retinue/feed.py, `ENDING_ACTIONS`);
  // the mock keeps the word after an answer, so every width of the e2e run finds it
  const ended = (kind: string) =>
    [...sessions.values()].filter((s) => fx.endings[s.id]?.kind === kind).map((s) => row(s, { key: `ending:${s.id}`, source: "session", since: s.at, essence: endingLine(fx.endings[s.id]!), ending: fx.endings[s.id] }));
  const read = [...sessions.values()].filter((s) => s.state === "idle" && !fx.endings[s.id]);
  const offered = [...proposals.values()].filter((p) => p.state === "open" && sessions.get(p.session));
  const writes = waitingProposals().filter((p) => p.session && sessions.get(p.session));
  const last = (id: string) => [...summaries.values()].filter((x) => x.session === id).at(-1);
  const all: Feed["groups"] = [
    { action: "answer", title: "Answer the question", rows: [
      ...answer.map((a) => row(sessions.get(a.session)!, { key: `card:${a.id}`, action: "answer", source: "question", since: a.created, essence: "", approval: a, said: SAID })),
      ...ended("ask").map((r) => ({ ...r, action: "answer" as const })),
      ...laptopRows().filter((r) => r.action === "answer"),
    ] },
    { action: "decide", title: "Decide", rows: [
      ...ended("approve").map((r) => ({ ...r, action: "decide" as const })),
      ...decide.map((a) => row(sessions.get(a.session)!, { key: `card:${a.id}`, action: "decide", source: "approval", since: a.created, essence: "", approval: a })),
      ...offered.map((p) => row(sessions.get(p.session)!, { key: `proposal:${p.id}`, action: "decide", source: "proposal", since: p.created, essence: `Sub-session: ${p.title}`, proposal: p })),
      ...writes.map((p) => row(sessions.get(p.session!)!, { key: `cproposal:${p.id}`, action: "decide", source: "collection_proposal", since: p.created, essence: `${p.collection}: ${p.changes.length} change${p.changes.length === 1 ? "" : "s"} proposed`, collection_proposal: p })),
    ] },
    { action: "step", title: "Your step", rows: [...ended("step").map((r) => ({ ...r, action: "step" as const })), ...laptopRows().filter((r) => r.action === "step")] },
    { action: "reply", title: "Reply", rows: laptopRows().filter((r) => r.action === "reply") },
    { action: "read", title: "Read", rows: [
      ...[...ended("look"), ...ended("done")].map((r) => ({ ...r, action: "read" as const })),
      ...read.map((s) => row(s, { key: `read:${s.id}`, action: "read", source: "session", since: s.at, essence: last(s.id)?.text.split("\n")[0] ?? "" })),
      ...laptopRows().filter((r) => r.action === "read"),
    ] },
    // her rows as feed.py makes them of the digest: a reminder is the day's, not a queue's
    { action: "assistant", title: "From the assistant", rows: digest().filter((i) => i.kind !== "reminder" && i.since <= now()).map((i) => ({
      key: `assistant:${i.id}`, action: "assistant", source: i.kind, session: null, track: null, sphere: i.sphere, sphere_title: i.sphere,
      sphere_icon: null, project: null, title: i.who, since: i.since, essence: i.title, ref: i.id, ...(i.t ? { t: i.t } : {}),
    })) },
  ];
  const groups = all.filter((g) => g.rows.length);
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
  return { schema: 1, count: groups.reduce((n, g) => n + g.rows.length, 0), groups, bot: "retinue_mock_bot", assistant_at: now() - 600, deadlines, chips, closed_today: closed(), expired_week: 2 };
}

// --- state and the event log -----------------------------------------------------------------------------------

const sessions = new Map<string, Session>(fx.sessions.map((s) => [s.id, { ...s }]));
seedProposals(fx.sessions[0]!.id); // what its first session proposed to write to the «work» Space's deals
const items = new Map<string, Item>(fx.items().map((it) => [`${it.session}/${it.turn}/${it.n}`, it]));
const approvals = new Map<string, Approval>(fx.approvals.map((a) => [a.id, { ...a }]));
// keyed `<session>/<note seq>`, as the platform keys summary events
const baseCards = new Map(fx.baseCards.map((c) => [c.id, c]));
const proposals = new Map<string, Proposal>(fx.proposals.map((p) => [p.id, { ...p }]));
const summaries = new Map<string, Summary>(fx.summaries.map((s) => [`${s.session}/${s.seq}`, s]));
let noteSeq = 100;
const MOCK_PAGES = "https://pages.in.example.test";
// «Uploads» (retinue/collections.py): invented files, kept by their SHA-256 in memory; the bytes are dropped for the
// seeded ones, so a download of those is a few made-up bytes.
const UPLOAD_MAX = 40 * 1024 * 1024;
interface Uploaded { sha256: string; name: string; size: number; mime: string; created: number; bytes?: Buffer }
const uploads = new Map<string, Uploaded>(
  [
    { name: "acme-invoice-2026-09.pdf", size: 184_320, mime: "application/pdf", created: now() - 3 * 86400 },
    { name: "example-report.xlsx", size: 96_811, mime: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", created: now() - 3 * 86400 + 40 },
    { name: "acme-orders-export.csv", size: 12_044, mime: "text/csv", created: now() - 86400 },
    { name: "IMG_0412.HEIC", size: 2_411_903, mime: "image/heic", created: now() - 3600 },
    { name: "Screenshot 2026-10-30 at 12.04.11 — a very long name that wraps on a phone.png", size: 731_220, mime: "image/png", created: now() - 120 },
  ].map((f) => {
    const sha256 = createHash("sha256").update(f.name).digest("hex");
    return [sha256, { ...f, sha256 }];
  }),
);
const uploadRow = ({ bytes: _bytes, ...row }: Uploaded) => row;
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

/** The Router's push after a press: an open «Today» reads its plan again, and each collection its writes moved on. */
function told(moved: Moved[]) {
  emit("plan", "plan", { at: now() });
  for (const m of moved) emit("collection", m.name, m);
}

function delta(session: string, text: string) {
  for (const res of streams) send(res, "delta", { session, text });
}

// «Talk to Retinue»: the assistant's conversation; what she is writing goes out as `chat-draft`, never kept
const chat = mockChat(emit, (draft) => {
  for (const res of streams) send(res, "chat-draft", draft);
});

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
    proposals: [...proposals.values()].filter((p) => p.session === id).sort((a, b) => a.created - b.created),
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
    if (path === paths.auth("code") && method === "POST") {
      return String(b.code ?? "").toUpperCase() === CODE ? login(res) : failed(res, "The code did not work.");
    }
    if (path === paths.auth("telegram") && method === "POST") {
      // The real platform checks the HMAC, auth_date within 5 minutes and user.id; the mock only the shape.
      const data = new URLSearchParams(String(b.init_data ?? ""));
      return data.get("hash") && data.get("user") ? login(res) : failed(res, "Telegram did not confirm the login.");
    }
    if (path === paths.auth("logout") && method === "POST") {
      return json(res, 200, { ok: true }, { "Set-Cookie": `${COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0` });
    }
    return json(res, 404, { error: "Not found." });
  }
  if (!authed(req)) return json(res, 401, { error: "Log in first." });

  if (path === paths.whatsNewSeen && method === "POST") {
    fx.whatsNew.length = 0;
    return json(res, 200, { ok: true });
  }
  if (path === paths.meta) return json(res, 200, { schema: 1, owner_ok: true, tracks: fx.tracks, general: "notes", spheres: fx.spheres, face_origin: "http://127.0.0.1:5173", speech: true, version: "v0.2.0", whats_new: fx.whatsNew });

  if (path === paths.settings && method === "GET") return json(res, 200, settingsView());
  if (path.startsWith(`${paths.settings}/`) && method === "POST") {
    const [status, answer] = settingsPost(path.slice(paths.settings.length + 1), await body(req));
    return json(res, status, answer);
  }
  if (path === paths.chat && method === "GET") return json(res, 200, chat.snapshot());
  if (path === paths.chat && method === "POST") {
    const b = await body(req);
    if (b.attachments) return json(res, 400, { error: "The app's chat takes text for now: send files to the bot in Telegram." });
    const said = chat.say(String(b.input_id ?? ""), String(b.text ?? ""));
    return json(res, said.status, said.body);
  }
  if (path === paths.chatPress && method === "POST") {
    const b = await body(req);
    const pressed = chat.press(String(b.id ?? ""), String(b.row ?? ""));
    return json(res, pressed.status, pressed.body);
  }
  if (path.startsWith("/api/chat/files/") && method === "GET") {
    res.writeHead(200, { "Content-Type": "application/pdf", "Content-Disposition": 'attachment; filename="week-plan.pdf"' });
    return res.end("%PDF-1.4\n% an invented file\n");
  }

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

  if (path === paths.feed) return json(res, 200, mockFeed());
  if (path === paths.stats) return json(res, 200, mockStats());
  if (path === paths.plan) return json(res, 200, mockPlan());
  // A press is checked as the platform checks it (retinue/plan.py `clean_press`), then done as the Router does it.
  if (path === paths.planPress && method === "POST") {
    const press = cleanPress(await body(req));
    if (typeof press === "string") return json(res, 400, { error: press });
    told(mockPlanPress(press).moved);
    return json(res, 200, { ok: true });
  }
  if (path === "/api/_mock/assistant" && method === "POST") {
    const { plan, moved } = mockAssistantMove();
    told(moved);
    return json(res, 200, { plan });
  }
  if (path === paths.waitingPress && method === "POST") {
    const { t, action } = (await body(req)) as { t?: string; action?: string };
    if (typeof t !== "string" || !/^[0-9a-f]{32}$/.test(t) || (action !== "done" && action !== "dismissed" && action !== "later" && action !== "open"))
      return json(res, 400, { error: "Need t — the row's token — and action: done, dismissed, later or open." });
    pressRow(t, action);
    return json(res, 200, { ok: true });
  }

  if (path === paths.waitingDetail && method === "POST") {
    const { ref, t } = (await body(req)) as { ref?: string; t?: string };
    const row = digest().find((i) => i.id === ref && i.kind !== "reminder" && i.kind !== "deadline");
    if (typeof ref !== "string") return json(res, 400, { error: "Need ref — the row's id — and t, its token, on a person's row." });
    if (!row || (row.t && row.t !== t) || ref === "letter:4")
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
  const file = method === "GET" ? mockBase(path) : null;
  if (file) {
    res.writeHead(200, { "Content-Type": file.type, "Cache-Control": "no-store" });
    return res.end(file.body);
  }
  // «Pages»: synthetic pages on a made-up pages service; `open` and `done` kept in memory like the platform's table.
  // «Pages»' cases (docs/face.md «Pages»): MOCK_PAGES_OFF=1 not set up, MOCK_PAGES_DOWN=1 late, MOCK_PAGES_NONE=1 none published
  if (path === paths.library && method === "GET") {
    const env = process.env;
    if (env.MOCK_PAGES_OFF === "1") return json(res, 200, { schema: 1, origin: "", at: null, pages: [] });
    if (env.MOCK_PAGES_NONE === "1") return json(res, 200, { schema: 1, origin: MOCK_PAGES, at: now() - 120, pages: [] });
    return json(res, 200, { schema: 1, origin: MOCK_PAGES, at: now() - (env.MOCK_PAGES_DOWN === "1" ? 5 * 3600 : 120), pages: [...library.values()] });
  }
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

  // «Views»: a Space's collections as the platform computes them, made up here.
  const views = method === "GET" ? mockCollections(path, url.searchParams) : null;
  if (views) return json(res, views.status, views.data);
  if (path === "/api/_mock/collection" && method === "POST") {
    const moved = moveDeal();
    if (url.searchParams.get("quiet") !== "1") emit("collection", moved.name, moved);
    return json(res, 200, moved);
  }
  // …what the sessions proposed to write to them, and his press on it (docs/collections.md «From a session»).
  const decided = mockProposals(path, method, url.searchParams);
  if (decided) {
    if (decided.moved) emit("collection", decided.moved.name, decided.moved);
    if (decided.proposal) emit("collection_proposal", decided.proposal.id, decided.proposal);
    return json(res, decided.status, decided.data);
  }
  // …and the owner's writes to them, each at the version shown (docs/collections.md «From the screen»).
  // Only a collection's own path: its body is read once, and every other POST keeps its own for its route below.
  const wrote = method === "POST" && /^\/api\/c\/[a-z][a-z0-9_]*$/.test(path) && path !== "/api/c/_files"
    ? mockWrite(path, await body(req))
    : null;
  if (wrote) {
    if (wrote.moved) emit("collection", wrote.moved.name, wrote.moved);
    return json(res, wrote.status, wrote.data);
  }

  // «Uploads»: the bytes read whole (the real limit is nginx's and the platform's), a new file told to every stream.
  if (path === "/api/c/_files" && method === "GET")
    return json(res, 200, { schema: 1, max: UPLOAD_MAX, files: [...uploads.values()].map(uploadRow).sort((a, b) => b.created - a.created) });
  if (path === "/api/c/_files" && method === "POST") {
    const chunks: Buffer[] = [];
    let total = 0;
    for await (const chunk of req) {
      total += (chunk as Buffer).length;
      if (total > UPLOAD_MAX) return json(res, 413, { error: "The file is too large: 40 MB at most." });
      chunks.push(chunk as Buffer);
    }
    const bytes = Buffer.concat(chunks);
    if (!bytes.length) return json(res, 400, { error: "The file is empty." });
    await new Promise((done) => setTimeout(done, 400)); // a slow link, so the progress shows
    const sha256 = createHash("sha256").update(bytes).digest("hex");
    const known = uploads.get(sha256);
    if (known) return json(res, 200, { file: uploadRow(known), new: false });
    let name = "file";
    try {
      name = decodeURIComponent(String(req.headers["x-name"] ?? "")).split(/[/\\]/).pop()?.trim() || "file";
    } catch {
      // a name that is not percent-encoded UTF-8: «file»
    }
    const row: Uploaded = { sha256, name, size: bytes.length, mime: String(req.headers["content-type"] ?? "application/octet-stream"), created: now(), bytes };
    uploads.set(sha256, row);
    emit("upload", sha256, uploadRow(row));
    return json(res, 200, { file: uploadRow(row), new: true });
  }
  const uploaded = /^\/api\/c\/_files\/([0-9a-f]{64})$/.exec(path);
  if (uploaded && method === "GET") {
    const file = uploads.get(uploaded[1] ?? "");
    if (!file) return json(res, 404, { error: "No such file." });
    res.writeHead(200, { "Content-Type": file.mime, "Content-Disposition": `attachment; filename*=UTF-8''${encodeURIComponent(file.name)}`, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store" });
    return res.end(file.bytes ?? Buffer.from(`made-up bytes of ${file.name}\n`));
  }

  if (path === paths.sessions && method === "GET") return json(res, 200, { schema: 1, cursor: lastId, sessions: [...sessions.values()] });
  // a made-up laptop's own sessions (retinue/computers.py, `laptop` above)
  if (path === paths.computers && method === "GET")
    return json(res, 200, {
      off_s: 120,
      computers: [{ device: "laptop", at: now() - (LAPTOP_OFF ? 3600 : 5), off: LAPTOP_OFF, sessions: laptop().map(({ ending: _ending, ...s }) => s) }],
    });
  if (path === paths.archived && method === "GET")
    return json(res, 200, { schema: 1, sessions: [...sessions.values()].filter((s) => s.archived).map((s) => ({ ...s, archived_at: s.at })).sort((a, b) => b.archived_at - a.archived_at) });

  if (path === paths.sessions && method === "POST") {
    const b = await body(req);
    const key = String(b.input_id ?? "");
    if (seenInputs.has(key)) return json(res, 200, seenInputs.get(key));
    const sphere = fx.spheres.find((x) => x.name === b.sphere);
    // `main: true`: into the sphere's main session, as `/<sphere> text` (retinue/spheres.py `command`)
    const main = b.main === true && sphere ? [...sessions.values()].find((s) => s.sphere === sphere.name && s.main && !s.archived) : undefined;
    if (b.main === true && !sphere) return json(res, 400, { error: "input_id (face:<uuid4>), sphere, text: invalid" });
    if (main && String(b.text ?? "").trim()) {
      const input = { session: main.id, input_id: key, text: String(b.text), at: now(), turn: null, n: null, attachments: [] };
      inputs.set(`${main.id}/${key}`, input);
      emit("input", `${main.id}/${key}`, input);
      const out = { session: main.id, say: "Accepted." };
      seenInputs.set(key, out);
      void simulateTurn(main.id, String(b.text));
      return json(res, 200, out);
    }
    const track = fx.tracks.find((t) => t.name === (b.track ?? sphere?.track));
    if (!track || !String(b.text ?? "").trim()) return json(res, 400, { error: "A track and a text are needed." });
    const id = randomBytes(4).toString("hex");
    const text = String(b.text);
    const made: Session = { id, track: track.name, lane: track.lane, kind: track.kind, state: "active", created: now(), at: now(), title: text.slice(0, 80), waiting: 0, sphere: sphere?.name ?? null, main: b.main === true, ...(typeof b.project === "string" && b.project ? { project: b.project } : {}) };
    sessions.set(id, { ...made, slot: fx.freeSlot(sessions.values(), made) }); // its place on the map (retinue/sessions.py `take_slot`)
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

  const offer = /^\/api\/proposals\/([0-9a-f]{8})\/(start|dismiss)$/.exec(path);
  if (offer && method === "POST") {
    const p = proposals.get(offer[1]!);
    if (!p) return json(res, 404, { error: "No such proposal." });
    if (offer[2] === "dismiss") {
      const next = p.state === "open" ? { ...p, state: "declined" as const, at: now() } : p;
      proposals.set(p.id, next);
      emit("proposal", p.id, next);
      return json(res, 200, { proposal: next });
    }
    if (p.state === "started") return json(res, 200, { proposal: p, say: `Started already: sub-session ${p.child}.` });
    if (p.state !== "open") return json(res, 409, { error: "You set this proposal aside." });
    const parent = sessions.get(p.session)!;
    const id = randomBytes(4).toString("hex");
    sessions.set(id, { id, track: parent.track, lane: parent.lane, kind: parent.kind, state: "active", created: now(), at: now(), title: p.title, waiting: 0, sphere: parent.sphere ?? null, sub_of: parent.id });
    emit("session", id, sessions.get(id));
    const next = { ...p, state: "started" as const, child: id, at: now() };
    proposals.set(p.id, next);
    emit("proposal", p.id, next);
    void simulateTurn(id, p.brief);
    return json(res, 200, { proposal: next, say: `Sub-session ${id} "${p.title}" started.` });
  }

  const pc = /^\/api\/push\/([0-9a-z]{8})(\/diff)?$/.exec(path);
  if (pc && method === "GET") {
    const card = fx.pushCards.find((p) => p.id === pc[1]);
    if (!card) return json(res, 404, { error: "No such card." });
    return json(res, 200, pc[2] ? { diff: fx.PUSH_DIFF, truncated: false } : { card });
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
      setSession(id, { state: "closed", archived: true, main: false, waiting: 0 });
      return json(res, 200, { session: sessions.get(id) });
    }
    if (sub === "/unarchive" && method === "POST") {
      const b = await body(req);
      const { archived: _, ...back } = sessions.get(id)!;
      sessions.set(id, { ...back, main: b.main === true, at: now() });
      emit("session", id, sessions.get(id));
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
