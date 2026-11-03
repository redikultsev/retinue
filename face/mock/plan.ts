// «Today»'s tasks, day and week, made up: the collection `tasks` in memory, a calendar of the next two weeks, the
// digest's asks and reminders — `GET /api/plan`, the owner's writes to `tasks`, «Make it a task», and a task's record
// for the drawer. Invented names only.

type Fields = Record<string, unknown>;
interface Rec { id: string; fields: Fields; version: number; created: number; updated: number; deleted: number | null }

const now = () => Date.now() / 1000;
const TZ = "Europe/Berlin";
const GOALS = [
  { value: "work", label: "Work" },
  { value: "home", label: "Home" },
  { value: "health", label: "Health" },
  { value: "study", label: "Study" },
];
const FORM = {
  title: { type: "text", required: true },
  goal: { type: "enum", values: GOALS.map((g) => g.value), labels: Object.fromEntries(GOALS.map((g) => [g.value, g.label])) },
  state: { type: "enum", values: ["open", "done", "dropped"], required: true, labels: { open: "Open", done: "Done", dropped: "Dropped" } },
  due: { type: "date" },
  minutes: { type: "int" },
  ask: { type: "text", hide: true },
  notes: { type: "text" },
};

let seq = 0;
let rev = "01K00000000000000000000000";
const id = () => `01KTASK${String(++seq).padStart(19, "0")}`;
const bump = () => {
  rev = `01K${Date.now().toString(32).toUpperCase().padStart(10, "0")}${String(++seq).padStart(13, "0")}`.slice(0, 26);
  return rev;
};

/** A moment `days` from today at a wall time in Berlin (+1 in winter), as a slot keeps it. */
function wall(days: number, h: number, m = 0): number {
  const d = new Date(Date.now() + 3600_000);  // Berlin's day, not UTC's
  d.setUTCHours(h - 1, m, 0, 0);
  return d.getTime() / 1000 + days * 86400;
}
const iso = (ts: number) => new Date(Math.round(ts) * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
const day = (days: number) => new Date((now() + 3600 + days * 86400) * 1000).toISOString().slice(0, 10);

const tasks = new Map<string, Rec>();
function add(fields: Fields): Rec {
  const rec: Rec = { id: id(), fields: { state: "open", ...fields }, version: 1, created: now() - 86400 * seq, updated: now(), deleted: null };
  tasks.set(rec.id, rec);
  return rec;
}
add({ title: "Reply to Anna Petrova about the contract", goal: "work", minutes: 15, due: day(2) });
add({ title: "Call Example Studio about the kitchen quote", goal: "work", minutes: 30 });
add({ title: "Choose a hotel for the weekend from five options", goal: "home", minutes: 60, due: day(5) });
add({ title: "Book the dentist", goal: "health", minutes: 15 });
add({ title: "Ask Ivan for the soup recipe", minutes: 15 });
const pay = add({ title: "Pay the hosting invoice", goal: "home", minutes: 15, ask: "letter:2" });
const guitar = add({ title: "Practise the guitar piece", goal: "study", minutes: 90, due: day(1) });
const report = add({ title: "Write the quarterly report, first half", goal: "work", minutes: 120 });
const domain = add({ title: "Renew the domain", goal: "home", minutes: 15, state: "done" });

interface Blk { id: string; fields: Fields; version: number }
const blocks = new Map<string, Blk>();
function block(task: Rec, start: number, minutes: number, extra: Fields = {}): Blk {
  const b: Blk = { id: id(), fields: { task: task.id, start: iso(start), minutes, state: "planned", ...extra }, version: 1 };
  blocks.set(b.id, b);
  return b;
}
block(pay, wall(0, 18, 0), 15, { why: "overdue" });
block(guitar, wall(1, 17, 30), 90, { locked: true });
block(report, wall(2, 19, 0), 90);
block(domain, wall(0, 9, 0), 15, { state: "passed" });
const plans: Record<string, unknown> = {};
const recent: { id: string; by: "owner" | "assistant"; at: number; undone: boolean; failed: boolean }[] = [];
const holds: { from: number; to: number; label: string }[] = [];
let said: { at?: number; text?: string; day?: string } = {};

const EVENTS = [
  [0, 8, 0, 8, 30, "Daily stand-up", true],
  [0, 12, 0, 13, 0, "Lunch with Acme", true],
  [0, 20, 0, 20, 45, "Gym", false],
  [1, 8, 0, 8, 30, "Daily stand-up", true],
  [1, 18, 0, 19, 0, "Guitar lesson", true],
  [2, 8, 0, 8, 30, "Daily stand-up", true],
  [2, 10, 0, 11, 0, "Sprint planning", true],
  [3, 8, 0, 8, 30, "Daily stand-up", true],
  [4, 16, 0, 17, 0, "Call with Example Studio", true],
  [5, 11, 0, 13, 0, "Market with friends", true],
] as const;

function calendar() {
  const events: { start: number; end: number; title: string; all_day: boolean; busy: boolean }[] = EVENTS.map(
    ([d, h1, m1, h2, m2, title, busy]) => ({ start: wall(d, h1, m1), end: wall(d, h2, m2), title, all_day: false, busy }),
  );
  const trip = wall(6, 1);
  events.push({ start: trip - (trip % 86400), end: trip - (trip % 86400) + 2 * 86400, title: "Trip", all_day: true, busy: false });
  return { events: events.sort((a, b) => a.start - b.start), at: now() - 240, failed: 0, stale: false };
}

const ASKS = [
  { id: "chat:1", kind: "chat", who: "Anna Petrova", title: "reply: is an airport transfer needed", since: now() - 6 * 3600, t: "a".repeat(32) },
  { id: "letter:2", kind: "letter", who: "Acme Hosting", title: "pay: the hosting invoice for May · deadline Friday", since: now() - 2 * 86400, t: "b".repeat(32) },
  { id: "letter:5", kind: "letter", who: "Example Studio", title: "reply: a second call next week", since: now() - 86400, t: "e".repeat(32) },
];
const REMINDERS = [
  { id: "reminder:7", title: "Send the report draft to Acme", at: wall(3, 19, 0), when: day(3), repeat: null },
  { id: "reminder:8", title: "Water the plants", at: wall(1, 16, 0), when: day(1), repeat: "weekly" },
];

export function mockPlan() {
  return {
    schema: 1,
    now: now(),
    tz: TZ,
    tasks: {
      rows: [...tasks.values()].filter((t) => !t.deleted),
      rev,
      space: "home",
      goals: GOALS,
      missing: false,
      blocks: [...blocks.values()].filter((b) => b.fields.state !== "skipped"),
    },
    calendar: calendar(),
    asks: ASKS,
    reminders: REMINDERS,
    plan: { at: now(), plans, recent: recent.slice(0, 20), holds: holds.filter((h) => h.to > now()), said, window: { zone: "Asia/Dubai", opens: "10:00", closes: "01:00" } },
  };
}

/** `POST /api/c/tasks`: add, set and revert as the platform does them — or null for another path. */
export function mockTasksWrite(path: string, body: Record<string, unknown>) {
  if (path !== "/api/c/tasks") return null;
  const moved = { name: "tasks", space: "home", rev };
  const ops = Array.isArray(body.ops) ? (body.ops as Fields[]) : [];
  const answer = { name: "tasks", rev: null as string | null, change_ids: [] as (string | null)[], records: [] as Rec[] };
  for (const op of ops) {
    if (op.op === "add") {
      const rec = add(op.fields as Fields);
      answer.records.push(rec);
      answer.change_ids.push(bump());
      continue;
    }
    const rec = tasks.get(String(op.id));
    if (!rec) return { status: 400, data: { error: "no such task", kind: null, record: null, change: null }, moved };
    if (rec.version !== op.version) return { status: 409, data: { error: "it moved on", kind: "stale", record: rec, change: null }, moved };
    if (op.op === "set") {
      for (const [k, v] of Object.entries(op.fields as Fields)) {
        if (v === null) delete rec.fields[k];
        else rec.fields[k] = v;
      }
    } else if (op.op === "delete") rec.deleted = now();
    rec.version += 1;
    rec.updated = now();
    answer.records.push(rec);
    answer.change_ids.push(bump());
  }
  answer.rev = rev;
  return { status: 200, data: answer, moved: { ...moved, rev } };
}

/** `GET /api/c/tasks/_form` and `/api/c/tasks/<id>`: the form, a task's record for its drawer. */
export function mockTasksRead(path: string) {
  if (path === "/api/c/tasks/_form") return { status: 200, data: { name: "tasks", rev, fields: FORM, options: {} } };
  const m = /^\/api\/c\/tasks\/([0-9A-Z]{26})$/.exec(path);
  if (!m) return null;
  const rec = tasks.get(m[1] ?? "");
  if (!rec) return { status: 404, data: { error: "No such view or record." } };
  return { status: 200, data: { name: "tasks", rev, title: rec.fields.title, fields: FORM, record: rec, changes: [], words: {} } };
}

/** «Make it a task» on an ask or a reminder: made at once here, as the Router does a moment later. */
export function mockMakeTask(by: { t?: string; ref?: string }): boolean {
  const ask = ASKS.find((a) => a.t === by.t);
  const reminder = REMINDERS.find((r) => r.id === by.ref);
  if (!ask && !reminder) return false;
  const name = ask ? ask.id : (reminder as { id: string }).id;
  if ([...tasks.values()].some((t) => t.fields.ask === name && t.fields.state === "open")) return true;
  add({ title: ask ? ask.title : reminder?.title, ask: name, ...(reminder?.when ? { due: reminder.when } : {}) });
  bump();
  return true;
}

/** The plan zone's day a moment's window is (an hour back: 00:30 is the evening before); the zone is UTC+4. */
const windowDay = (ts: number) => new Date((ts + 4 * 3600 - 3600) * 1000).toISOString().slice(0, 10);

/** `POST /api/blocks` and the owner's own blocks: add or move, as the platform writes them. */
export function mockBlocksWrite(path: string, body: Record<string, unknown>) {
  if (path !== "/api/c/blocks") return null;
  const ops = Array.isArray(body.ops) ? (body.ops as Fields[]) : [];
  const records: Blk[] = [];
  for (const op of ops) {
    if (op.op === "add") records.push(block(tasks.get(String((op.fields as Fields).task)) as Rec, Date.parse(String((op.fields as Fields).start)) / 1000, Number((op.fields as Fields).minutes), op.fields as Fields));
    else {
      const b = blocks.get(String(op.id));
      if (!b) return { status: 400, data: { error: "no such block", kind: null, record: null, change: null } };
      Object.assign(b.fields, op.fields as Fields);
      b.version += 1;
      records.push(b);
    }
  }
  return { status: 200, data: { name: "blocks", rev: bump(), change_ids: records.map(() => rev), records }, moved: { name: "blocks", space: "home", rev } };
}

const befores = new Map<string, Map<string, Fields>>();

/** The assistant's ask done, as the Router's Planner does it: the report's block to tomorrow evening and two hours long,
 * one plan with its «before» — `POST /api/_mock/assistant`. Its «Undo» is the plan's, as a press of the owner's. */
export function mockAssistantMove(): string {
  const t = now();
  const b = [...blocks.values()].find((x) => x.fields.task === report.id && x.fields.state === "planned");
  if (!b) return "";
  const pid = `p${Math.random().toString(16).slice(2, 12).padEnd(10, "0")}`;
  befores.set(pid, new Map([[b.id, { start: b.fields.start, minutes: b.fields.minutes, plan: b.fields.plan ?? null }]]));
  const from = String(b.fields.start);
  Object.assign(b.fields, { start: iso(wall(1, 20, 0)), minutes: 120, plan: pid });
  b.version += 1;
  recent.unshift({ id: pid, by: "assistant", at: t, undone: false, failed: false });
  said = { at: t, text: `The assistant moved «${report.fields.title}» from ${from.slice(0, 16)} to tomorrow 20:00, 2 h.` };
  bump();
  return pid;
}

/** A press on the plan, done at once here as the Router does it a moment later: open tasks in a row from 19:00. */
export function mockPlanPress(body: Record<string, unknown>): string {
  const t = now();
  const planned = new Set([...blocks.values()].filter((b) => b.fields.state === "planned").map((b) => b.fields.task));
  if (body.action === "plan" || body.action === "place") {
    const pid = `p${Math.random().toString(16).slice(2, 12).padEnd(10, "0")}`;
    const first = String(body.day ?? windowDay(t));
    const made: string[] = [];
    for (let n = 0; n < (body.days === 7 ? 7 : 1); n++) {
      const d = new Date(Date.parse(`${first}T12:00:00Z`) + n * 86400_000).toISOString().slice(0, 10);
      let at = Math.max(Date.parse(`${d}T15:00:00Z`) / 1000, Math.ceil(t / 900) * 900);
      const here: string[] = [];
      for (const task of tasks.values()) {
        if (task.fields.state !== "open" || planned.has(task.id) || (body.action === "place" && task.id !== body.task)) continue;
        const m = Math.min(Number(task.fields.minutes ?? 30), 90);
        here.push(block(task, at, m, { plan: pid }).id);
        planned.add(task.id);
        at += m * 60 + 900;
        if (here.length === 2) break;
      }
      made.push(...here);
      // What did not fit this window: the open tasks still with nothing planned, as the Router names them.
      const left = [...tasks.values()]
        .filter((x) => x.fields.state === "open" && !planned.has(x.id) && body.action === "plan")
        .map((x) => ({ task: x.id, title: String(x.fields.title), why: "no free 30 min before the window ends" }));
      plans[d] = { id: pid, at: t, blocks: here, left: body.days === 7 ? [] : left, undone: false, failed: false };
    }
    recent.unshift({ id: pid, by: "owner", at: t, undone: false, failed: false });
    said = { at: t, text: body.days === 7 ? `Week planned: ${made.length} blocks.` : `Plan for ${first}: ${made.length} tasks.`, day: first };
  } else if (body.action === "undo") {
    const was = befores.get(String(body.plan));
    for (const b of blocks.values()) {
      if (b.fields.plan !== body.plan || b.fields.state !== "planned") continue;
      const back = was?.get(b.id);
      if (back) {
        Object.assign(b.fields, back);
        b.version += 1;
      } else b.fields.state = "skipped";
    }
    for (const v of Object.values(plans)) if ((v as { id: string }).id === body.plan) (v as { undone: boolean }).undone = true;
    for (const v of recent) if (v.id === body.plan) v.undone = true;
    said = { at: t, text: "Taken back." };
  } else if (body.action === "busy") {
    holds.push({ from: t, to: t + Number(body.minutes ?? 60) * 60, label: "Busy" });
    said = { at: t, text: "Busy." };
  } else if (body.action === "free") {
    holds.length = 0;
  } else if (body.action === "remove") {
    const b = blocks.get(String(body.block));
    if (b) b.fields.state = "skipped";
  }
  bump();
  return said.text ?? "";
}
