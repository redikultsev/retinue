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

// --- «Ждут тебя» (retinue/feed.py): the shown cards, the idle sessions' last summaries, a fixed digest ----------

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
    { action: "answer", title: "Ответить на вопрос", rows: answer.map((a) => row(sessions.get(a.session)!, { key: `card:${a.id}`, action: "answer", source: "question", since: a.created, essence: "", approval: a })) },
    { action: "decide", title: "Решить", rows: decide.map((a) => row(sessions.get(a.session)!, { key: `card:${a.id}`, action: "decide", source: "approval", since: a.created, essence: "", approval: a })) },
    { action: "read", title: "Прочитать", rows: read.map((s) => row(s, { key: `read:${s.id}`, action: "read", source: "session", since: s.at, essence: last(s.id)?.text.split("\n")[0] ?? "" })) },
    { action: "assistant", title: "От ассистентки", rows: [
      { key: "assistant:chat:1", action: "assistant", source: "chat", session: null, track: null, sphere: "travel", sphere_title: "travel", sphere_icon: null, title: "Анна Петрова", since: now() - 6 * 3600, essence: "ответить: нужен ли трансфер из аэропорта", ref: "chat:1" },
      { key: "assistant:deadline:1", action: "assistant", source: "deadline", session: null, track: null, sphere: "home", sphere_title: "home", sphere_icon: null, title: "База", since: now() - 86400, essence: "завтра: Оплатить интернет — срок", ref: "deadline:1" },
    ] },
  ].filter((g) => g.rows.length);
  const day = (n: number) => new Date(Date.now() + n * 86400_000).toISOString().slice(0, 10);
  const deadlines = [
    { id: "deadline:1", when: day(1), title: "завтра: Оплатить интернет — срок", sphere: "home", sphere_title: "Дом", sphere_icon: "🏠", path: "home/journal/2026-05-01-internet.md" },
    { id: "deadline:2", when: day(5), title: "через 5 дней: Абонемент в спортзал — действует до", sphere: "home", sphere_title: "Дом", sphere_icon: "🏠", path: "home/profile/gym.md" },
  ];
  const chips = [
    { key: "health", text: "всё работает", level: "ok" },
    { key: "backup", text: "бэкап 3 ч назад", level: "ok" },
    { key: "limit", text: "лимит 74 %", level: "warn" },
    { key: "build", text: "хаб собран", level: "ok" },
  ];
  return { schema: 1, count: groups.reduce((n, g) => n + g.rows.length, 0), groups, bot: "retinue_mock_bot", assistant_at: now() - 600, deadlines, chips };
}

// --- state and the event log -----------------------------------------------------------------------------------

const sessions = new Map<string, Session>(fx.sessions.map((s) => [s.id, { ...s }]));
const items = new Map<string, Item>(fx.items().map((it) => [`${it.session}/${it.turn}/${it.n}`, it]));
const approvals = new Map<string, Approval>(fx.approvals.map((a) => [a.id, { ...a }]));
// keyed `<session>/<note seq>`, as the platform keys summary events
const summaries = new Map<string, Summary>(fx.summaries.map((s) => [`${s.session}/${s.seq}`, s]));
let noteSeq = 100;
const MOCK_HUB = "https://hub.example.test";
const library = new Map(
  [
    { id: "regex-course", title: "Regex за вечер", section: "education", section_title: "Учёба", section_icon: "🎓", minutes: 95, updated: "2026-10-27" },
    { id: "sql-cheatsheet", title: "SQL: шпаргалка", section: "education", section_title: "Учёба", section_icon: "🎓", minutes: 40, updated: "2026-10-20" },
    { id: "ensk-notes", title: "Энск: заметки к поездке", section: "travel", section_title: "Путешествия", section_icon: "✈️", minutes: 6, updated: "2026-10-22" },
    { id: "loose-page", title: "Страница без раздела", section: null, section_title: null, section_icon: null, minutes: null, updated: "2026-09-30" },
  ].map((p) => [p.id, { ...p, url: `${MOCK_HUB}/pages/${p.id}.html`, opened: null as number | null, done: null as number | null }]),
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
  const comment = /^Комментарий к (.+?):(\d+): /.exec(text);
  const reply = comment
    ? `Понял замечание к \`${comment[1]}\`, строка ${comment[2]}. Исправляю и прогоню тесты.`
    : `Принял: «${text.slice(0, 60)}». Смотрю, что можно сделать, и вернусь с итогом.`;
  for (const word of reply.split(/(?<= )/)) {
    delta(session, word);
    await sleep(90);
  }
  putItem(session, turn, 1, { kind: "message", data: { role: "assistant", text: reply } });
  putItem(session, turn, 2, { kind: "tool", data: { id: `toolu_${randomUUID().slice(0, 8)}`, tool: "Bash", input: { command: "git status --short" }, result: null, error: false } });
  await sleep(700);
  putItem(session, turn, 2, { kind: "tool", data: { id: "toolu_status", tool: "Bash", input: { command: "git status --short" }, result: " M retinue/gate.py\n M tests/test_gate.py\n?? docs/face.md", error: false } });
  const final = "Готово: правка на месте, тесты зелёные.";
  for (const word of final.split(/(?<= )/)) {
    delta(session, word);
    await sleep(90);
  }
  putItem(session, turn, 3, { kind: "message", data: { role: "assistant", text: final } });
  putItem(session, turn, 4, { kind: "result", data: { stop_reason: "end_turn", is_error: false, text: final, deferred: null } });
  const s = sessions.get(session)!;
  const summary = { session, seq: ++noteSeq, head: `Итог · ${s.track} · сессия ${session}`, text: final, at: now() };
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
    about: fx.about(session),
  };
}

async function route(req: IncomingMessage, res: ServerResponse) {
  const url = new URL(req.url ?? "/", "http://mock");
  const path = url.pathname;
  const method = req.method ?? "GET";

  if (method === "POST") {
    if (req.headers["x-retinue"] !== "1") return json(res, 403, { error: "Нет заголовка X-Retinue." });
    const origin = req.headers.origin;
    if (origin && !ORIGINS.has(origin)) return json(res, 403, { error: "Чужой Origin." });
  }

  // auth
  if (path.startsWith("/api/auth/")) {
    if (now() < lockedUntil) return json(res, 429, { error: "Слишком много попыток — подожди 10 минут." });
    const b = method === "POST" ? await body(req) : {};
    if (path === "/api/auth/code" && method === "POST") {
      return String(b.code ?? "").toUpperCase() === CODE ? login(res) : failed(res, "Код не подошёл.");
    }
    if (path === "/api/auth/telegram" && method === "POST") {
      // The real platform checks the HMAC, auth_date within 5 minutes and user.id; the mock only the shape.
      const data = new URLSearchParams(String(b.init_data ?? ""));
      return data.get("hash") && data.get("user") ? login(res) : failed(res, "Telegram не подтвердил вход.");
    }
    if (path === "/api/auth/logout" && method === "POST") {
      return json(res, 200, { ok: true }, { "Set-Cookie": `${COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0` });
    }
    return json(res, 404, { error: "Не найдено." });
  }
  if (!authed(req)) return json(res, 401, { error: "Нужно войти." });

  if (path === "/api/meta") return json(res, 200, { schema: 1, owner_ok: true, tracks: fx.tracks, general: "notes", spheres: fx.spheres, face_origin: "http://127.0.0.1:5173", speech: true });

  // The owner's voice: the recording is read and dropped; a canned transcript per mode (no ElevenLabs here).
  if (path === "/api/speech" && method === "POST") {
    let size = 0;
    for await (const chunk of req) size += (chunk as Buffer).length;
    if (!size) return json(res, 400, { error: "Запись пустая." });
    if (url.searchParams.get("mode") === "en") {
      const text = "So, um, I think the KV cache, uh, is the bottleneck";
      return json(res, 200, { text, message: `(голосом, дословно) ${text}` });
    }
    return json(res, 200, { text: "проверь KV cache и сделай eval для RAG" });
  }

  if (path === "/api/feed") return json(res, 200, mockFeed());

  const space = method === "GET" ? mockSpaces(path) : null;
  if (space) return json(res, space.status, space.data);
  // «Читать»: synthetic pages on a made-up hub; `open` and `done` kept in memory like the platform's table.
  if (path === "/api/library" && method === "GET") return json(res, 200, { schema: 1, hub: MOCK_HUB, at: now() - 120, pages: [...library.values()] });
  const reading = /^\/api\/library\/([a-z0-9-]+)\/(open|done)$/.exec(path);
  if (reading && method === "POST") {
    const page = library.get(reading[1] ?? "");
    if (!page) return json(res, 404, { error: "Такой страницы в библиотеке нет." });
    const b = await body(req);
    if (reading[2] === "open") page.opened = now();
    else if (typeof b.done === "boolean") page.done = b.done ? now() : null;
    else return json(res, 400, { error: "done: true или false." });
    return json(res, 200, { page });
  }

  if (path === "/api/sessions" && method === "GET") return json(res, 200, { schema: 1, cursor: lastId, sessions: [...sessions.values()] });

  if (path === "/api/sessions" && method === "POST") {
    const b = await body(req);
    const key = String(b.input_id ?? "");
    if (seenInputs.has(key)) return json(res, 200, seenInputs.get(key));
    const sphere = fx.spheres.find((x) => x.name === b.sphere);
    const track = fx.tracks.find((t) => t.name === (b.track ?? sphere?.track));
    if (!track || !String(b.text ?? "").trim()) return json(res, 400, { error: "Нужны трек и текст." });
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
    if (since < oldestKept) return json(res, 410, { error: "Курсор устарел — перезагрузи снимок." });
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

  let m = /^\/api\/sessions\/([0-9a-f]{8})(\/[a-z]+)?$/.exec(path);
  if (m) {
    const id = m[1]!;
    const sub = m[2];
    const s = sessions.get(id);
    if (!s) return json(res, 404, { error: "Нет такой сессии." });
    if (!sub && method === "GET") return json(res, 200, sessionSnapshot(id));
    if (sub === "/diff" && method === "GET") {
      if (s.kind !== "code") return json(res, 400, { error: "У этой сессии нет кода." });
      return json(res, 200, { cwd: `/work/${s.track}`, stat: fx.STAT, diff: fx.DIFF, truncated: false });
    }
    if (sub === "/input" && method === "POST") {
      const b = await body(req);
      const key = String(b.input_id ?? "");
      if (seenInputs.has(key)) return json(res, 200, seenInputs.get(key));
      const text = String(b.text ?? "").trim();
      const attached = Array.isArray(b.attachments) ? (b.attachments as { name?: string; data?: string }[]) : [];
      if (!text && !attached.length) return json(res, 400, { error: "Пустое сообщение." });
      const turn = nextTurn(id);
      const input: Input = {
        session: id, input_id: key, text, at: now(), turn, n: 0,
        attachments: attached.map((f) => ({ kind: "file", name: String(f.name ?? ""), size: Math.floor((f.data?.length ?? 0) * 3 / 4), what: `файл «${f.name}»`, thumb: false })),
      };
      inputs.set(`${id}/${key}`, input);
      emit("input", `${id}/${key}`, input);
      // A message while a card is open makes the card stale, as in Telegram.
      for (const a of approvals.values()) if (a.session === id && a.state === "shown") putApproval({ ...a, state: "stale", at: now(), view: { ...a.view, status: "Устарела: ты написал новое сообщение" } });
      const out = { say: "Принято." };
      seenInputs.set(key, out);
      void simulateTurn(id, text);
      return json(res, 200, out);
    }
    if (sub === "/stop" && method === "POST") {
      setSession(id, { state: "idle" });
      return json(res, 200, { ok: true });
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
        if (!s.sphere) return json(res, 400, { error: "Сначала выбери сессии сферу." });
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
      if (!text) return json(res, 400, { error: "Пустое сообщение." });
      const fork = randomBytes(4).toString("hex");
      sessions.set(fork, { ...s, id: fork, state: "active", created: now(), at: now(), title: text.slice(0, 80), waiting: 0, pinned: false, main: false, fork_of: id });
      emit("session", fork, sessions.get(fork));
      void simulateTurn(fork, text);
      return json(res, 200, { session: fork, say: `Сессия ${fork} · ${s.track}.` });
    }
    if (sub === "/side" && method === "POST") {
      // The real platform starts the side session with an excerpt of this one's last items; the mock only the shape.
      const b = await body(req);
      const key = String(b.input_id ?? "");
      if (seenInputs.has(key)) return json(res, 200, seenInputs.get(key));
      const text = String(b.text ?? "").trim();
      if (!text) return json(res, 400, { error: "Пустой вопрос." });
      const side = randomBytes(4).toString("hex");
      sessions.set(side, { ...s, id: side, state: "active", created: now(), at: now(), title: text.slice(0, 80), waiting: 0, pinned: false, parent: s.parent ?? id });
      emit("session", side, sessions.get(side));
      const out = { session: side, say: `Сессия ${side} · ${s.track}.` };
      seenInputs.set(key, out);
      void simulateTurn(side, text);
      return json(res, 200, out);
    }
  }

  m = /^\/api\/approvals\/([0-9a-f]{8})$/.exec(path);
  if (m && method === "GET") {
    const a = approvals.get(m[1]!);
    return a && a.state !== "pending" ? json(res, 200, { approval: a }) : json(res, 404, { error: "Нет такой карточки." });
  }

  m = /^\/api\/approvals\/([0-9a-f]{8})\/decide$/.exec(path);
  if (m && method === "POST") {
    const a = approvals.get(m[1]!);
    const b = await body(req);
    if (!a) return json(res, 404, { error: "Нет такой карточки." });
    if (a.kind !== "approval" || a.state !== "shown" || b.digest !== a.digest || (b.action !== "approve" && b.action !== "reject")) {
      return json(res, 200, { toast: "Карточка устарела — решение не принято.", approval: a });
    }
    const granted = b.action === "approve";
    const decided: Approval = { ...a, state: granted ? "granted" : "rejected", at: now(), view: { ...a.view, status: granted ? "Разрешено — выполняю." : "Отказано." } };
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
      const text = granted ? "Вызов выполнен: 13 passed. Карточка после рестарта нажимается один раз." : "Понял, не запускаю. Что сделать вместо этого?";
      for (const w of text.split(/(?<= )/)) {
        delta(s, w);
        await sleep(80);
      }
      putItem(s, turn, 0, { kind: "message", data: { role: "assistant", text } });
      putItem(s, turn, 1, { kind: "result", data: { stop_reason: "end_turn", is_error: false, text, deferred: null } });
      setSession(s, { state: sessions.get(s)!.kind === "code" ? "closed" : "idle" });
    })();
    return json(res, 200, { toast: granted ? "Разрешено." : "Отказано.", approval: decided });
  }

  // A handed-over file, with the headers the platform sends (retinue/face.py `file`, `view`).
  const f = /^\/api\/files\/([^/]+)(\/view)?$/.exec(path);
  if (f && method === "GET") {
    const found = fx.files.find((x) => x.file.id === f[1]);
    if (!found || (f[2] && !found.file.mime.startsWith("text/html"))) return json(res, 404, { error: "Нет такого файла." });
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
    if (!s) return json(res, 404, { error: "Нет такой сессии." });
    const command = "rm -rf /work/notes/.cache && du -sh /work/notes";
    const id = randomBytes(4).toString("hex");
    putApproval({
      id, session, tool_use_id: `toolu_${id}`, kind: "approval", state: "shown", tool: "Bash", input: { command }, cwd: `/work/${s.track}`,
      digest: randomBytes(32).toString("hex"),
      view: { head: [`Разрешение · ${s.track} · сессия ${session}`, "Инструмент: Bash", `Папка: /work/${s.track}`, `Ввод, ${command.length} знаков:`], body: command },
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

  return json(res, 404, { error: "Не найдено." });
}

createServer((req, res) => {
  route(req, res).catch((e) => {
    console.error(e);
    if (!res.headersSent) json(res, 500, { error: "Ошибка мока." });
  });
}).listen(PORT, "127.0.0.1", () => {
  console.log(`mock face API on http://127.0.0.1:${PORT} — login code: ${CODE}`);
  void streamForever("5566aabb");
});
