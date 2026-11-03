// «Today»'s tasks and schedule in the mock: `GET /api/plan` as retinue/plan.py reads it, and the Router's Planner
// (retinue/planner.py) behind `POST /api/plan/press`. `tasks` and `blocks` are collections of the mock's store
// (mock/collections.ts): the owner writes them through the collections' route, as any screen does, and the Router's
// writes are `code:plan`'s. Beside them a calendar of the next two weeks, the digest's reminders
// (mock/digest.ts), and the day plan's state — its plans, holds, window, activities and the places the meals keep. A
// press is checked as plan.py checks it and done at once, as the Router does it a moment later — simpler: the free time
// it finds is not the Router's layout. Invented names only.
import { PRESS_FIELDS, PRESSES, type Activity, type Block, type Plan, type PlanWindow, type Press, type Task } from "../src/lib/contract.ts";
import type { CollectionRecord, FieldSpec, Op, WriteAnswer } from "../src/lib/view-types.ts";
import { define, type Moved, recordOf, recordsOf, revOf, seed, writeAs } from "./collections.ts";
import { digest } from "./digest.ts";

type Fields = Record<string, unknown>;

const now = () => Date.now() / 1000;
const TZ = "Europe/Berlin";
const SCHEMA = 2;
const ROUTER = "code:plan";
const GOALS = [
  { value: "work", label: "Work" },
  { value: "home", label: "Home" },
  { value: "health", label: "Health" },
  { value: "study", label: "Study" },
];

// The forms of deploy/collections/tasks.json and blocks.json.
const TASK_FIELDS: Record<string, FieldSpec> = {
  title: { type: "text", required: true },
  goal: { type: "enum", values: GOALS.map((g) => g.value), labels: Object.fromEntries(GOALS.map((g) => [g.value, g.label])) },
  project: { type: "text" },
  state: { type: "enum", values: ["open", "done", "dropped"], required: true, labels: { open: "Open", done: "Done", dropped: "Dropped" } },
  due: { type: "date" },
  by: { type: "instant" },
  minutes: { type: "int" },
  ask: { type: "text", hide: true },
  notes: { type: "text" },
};
const BLOCK_FIELDS: Record<string, FieldSpec> = {
  task: { type: "ref", to: "tasks", required: true },
  start: { type: "instant", required: true },
  minutes: { type: "int", required: true },
  state: { type: "enum", values: ["planned", "passed", "skipped"], required: true, labels: { planned: "Planned", passed: "Passed", skipped: "Skipped" } },
  locked: { type: "bool" },
  plan: { type: "text", hide: true },
  why: { type: "text" },
};
define("tasks", { space: "home", fields: TASK_FIELDS, title: (r) => String(r.fields.title ?? "") || "Untitled" });
define("blocks", {
  space: "home",
  fields: BLOCK_FIELDS,
  title: (r) => [recordOf("tasks", String(r.fields.task))?.fields.title, String(r.fields.start ?? "").slice(0, 16).replace("T", " ")].filter(Boolean).join(" · "),
});

// --- time in a zone ----------------------------------------------------------------------------------------------

const PARTS = new Map<string, Intl.DateTimeFormat>();
function parts(ts: number, tz: string) {
  let f = PARTS.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric" });
    PARTS.set(tz, f);
  }
  const got: Record<string, string> = {};
  for (const p of f.formatToParts(new Date(ts * 1000))) got[p.type] = p.value;
  return { y: Number(got.year), m: Number(got.month), d: Number(got.day), h: Number(got.hour) % 24, min: Number(got.minute) };
}
const pad = (n: number) => String(n).padStart(2, "0");
const dayIn = (ts: number, tz: string) => {
  const p = parts(ts, tz);
  return `${p.y}-${pad(p.m)}-${pad(p.d)}`;
};
const hmIn = (ts: number, tz: string) => {
  const p = parts(ts, tz);
  return `${pad(p.h)}:${pad(p.min)}`;
};
const minutesOf = (hhmm: string) => Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5));
const addDays = (day: string, n: number) => new Date(Date.parse(`${day}T12:00:00Z`) + n * 86400_000).toISOString().slice(0, 10);
/** The moment a wall time of a day is in a zone. */
function wallAt(day: string, minutes: number, tz: string): number {
  const wall = Date.parse(`${day}T00:00:00Z`) / 1000 + minutes * 60;
  let guess = wall;
  for (let i = 0; i < 3; i++) {
    const p = parts(guess, tz);
    guess += wall - Date.UTC(p.y, p.m - 1, p.d, p.h, p.min) / 1000;
  }
  return guess;
}
/** A moment `days` from today at a wall time in the owner's zone, as the calendar and the slots keep it. */
const wall = (days: number, h: number, m = 0) => wallAt(addDays(dayIn(now(), TZ), days), h * 60 + m, TZ);
const iso = (ts: number) => new Date(Math.round(ts) * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
const day = (days: number) => addDays(dayIn(now(), TZ), days);
const pid = () => `p${Math.random().toString(16).slice(2, 12).padEnd(10, "0")}`;

// --- the Router's state of the plan --------------------------------------------------------------------------------

/** The window a plan lays into: wall times in its zone, a close before the open after midnight. */
let window: PlanWindow = { zone: "Asia/Dubai", opens: "10:00", closes: "01:00" };
/** The Router's defaults until the owner sets his own (docs/collections.md «Blocks»). */
let activities: Activity[] = [
  { title: "Breakfast", minutes: 20, from: "10:00", to: "11:30" },
  { title: "Lunch", minutes: 45, from: "13:00", to: "16:00" },
  { title: "Dinner", minutes: 45, from: "19:00", to: "22:00" },
  { title: "Rest", minutes: 60 },
  { title: "Travel", minutes: 30 },
];
interface Hold {
  from: number;
  to: number;
  label: string;
  /** «Not working today»'s: the plan whose «Undo» takes it off. */
  plan?: string;
}
let holds: Hold[] = [];
const plans: Plan["plan"]["plans"] = {};
const recent: NonNullable<Plan["plan"]["recent"]> = [];
let said: Plan["plan"]["said"] = {};
/** What each plan wrote, by block: the version it left and the fields before it — null for a block it added. */
const written = new Map<string, Map<string, { version: number; before: Fields | null }>>();

function windowOf(d: string): [number, number] {
  const opens = minutesOf(window.opens);
  const closes = minutesOf(window.closes);
  return [wallAt(d, opens, window.zone), wallAt(closes <= opens ? addDays(d, 1) : d, closes, window.zone)];
}
/** The window a moment belongs to: one past midnight belongs to the evening before. */
function windowDay(ts: number): string {
  const d = dayIn(ts, window.zone);
  const closes = minutesOf(window.closes);
  return closes <= minutesOf(window.opens) && minutesOf(hmIn(ts, window.zone)) < closes ? addDays(d, -1) : d;
}

/** The places the meals keep, from yesterday's window a week on: at the first of each range. */
function meals(t: number): { title: string; from: number; to: number }[] {
  const first = addDays(windowDay(t), -1);
  return Array.from({ length: 9 }, (_, n) => addDays(first, n)).flatMap((d) =>
    activities.flatMap((a) => {
      if (!a.from || !a.to) return [];
      const from = wallAt(d, minutesOf(a.from), window.zone);
      return [{ title: a.title, from, to: from + a.minutes * 60 }];
    }),
  );
}

// --- the records ---------------------------------------------------------------------------------------------------

/** What the stream is told of the Router's writes, kept for the press's answer. */
const told: Moved[] = [];
function routerWrite(collection: "tasks" | "blocks", ops: Op[], actor = ROUTER): CollectionRecord[] {
  if (!ops.length) return [];
  const wrote = writeAs(actor, collection, ops);
  if (wrote.moved) told.push(wrote.moved);
  return wrote.status === 200 ? (wrote.data as WriteAnswer).records : [];
}
const blocks = () => recordsOf("blocks");
const spanOf = (b: CollectionRecord): [number, number] => {
  const start = Date.parse(String(b.fields.start)) / 1000;
  return [start, start + Number(b.fields.minutes) * 60];
};

const task = (fields: Fields, ageDays: number) => seed("tasks", { state: "open", ...fields }, ageDays);
const block = (of: CollectionRecord, start: number, minutes: number, extra: Fields = {}) =>
  seed("blocks", { task: of.id, start: iso(start), minutes, state: "planned", ...extra }, 0, ROUTER);
task({ title: "Reply to Anna Petrova about the contract", goal: "work", project: "work/acme", minutes: 15, due: day(2) }, 1);
task({ title: "Call Example Studio about the kitchen quote", goal: "work", minutes: 30 }, 2);
task({ title: "Put the new mockup on the site's front page", goal: "work", project: "work/acme/site", minutes: 45, due: day(3) }, 3);
task({ title: "Choose a hotel for the weekend from five options", goal: "home", project: "travel", minutes: 60, due: day(5) }, 4);
task({ title: "Book the dentist", goal: "health", minutes: 15 }, 5);
task({ title: "Ask Ivan for the soup recipe", minutes: 15 }, 6);
const pay = task({ title: "Pay the hosting invoice", goal: "home", project: "home", minutes: 15, ask: "letter:2" }, 7);
const guitar = task({ title: "Practise the guitar piece", goal: "study", minutes: 90, due: day(1) }, 8);
const report = task({ title: "Write the quarterly report, first half", goal: "work", project: "work/acme", minutes: 120, by: iso(wall(2, 18, 0)) }, 9);
const domain = task({ title: "Renew the domain", goal: "home", minutes: 15, state: "done" }, 10);
block(pay, wall(0, 18, 0), 15, { why: "overdue" });
block(guitar, wall(1, 17, 30), 90, { locked: true });
const half = block(report, wall(2, 14, 0), 90);
block(domain, wall(0, 9, 0), 15, { state: "passed" });
// Of the report's two hours only ninety fit before 18:00 that day: the plan says what is short.
plans[day(2)] = { id: "p0000000001", at: now(), blocks: [half.id], left: [], undone: false, failed: false,
                  short: [{ task: report.id, title: String(report.fields.title), minutes: 30 }] };

// --- the calendar --------------------------------------------------------------------------------------------------

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

function calendar(): Plan["calendar"] {
  const events: Plan["calendar"]["events"] = EVENTS.map(([d, h1, m1, h2, m2, title, busy]) => ({ start: wall(d, h1, m1), end: wall(d, h2, m2), title, all_day: false, busy }));
  const trip = wall(6, 1);
  events.push({ start: trip - (trip % 86400), end: trip - (trip % 86400) + 2 * 86400, title: "Trip", all_day: true, busy: false });
  return { events: events.sort((a, b) => a.start - b.start), at: now() - 240, failed: 0, stale: false };
}

// --- `GET /api/plan` -----------------------------------------------------------------------------------------------

/** What the tabs show, put together as plan.py does on each read: the open tasks and every task with a block from two
 * days back to two weeks on, those blocks still planned or passed, the calendar, the digest's reminders, and
 * the plan's state. */
export function mockPlan(): Plan & { schema: number } {
  const t = now();
  const [from, to] = [t - 2 * 86400, t + 15 * 86400];
  const near = blocks().filter((b) => ["planned", "passed"].includes(String(b.fields.state)) && spanOf(b)[0] >= from && spanOf(b)[0] <= to);
  const rows = new Map(recordsOf("tasks").filter((r) => r.fields.state === "open").map((r) => [r.id, r]));
  for (const b of near) {
    const of = recordOf("tasks", String(b.fields.task));
    if (of && of.deleted === null) rows.set(of.id, of);
  }
  const items = digest();
  const plan: Plan = {
    now: t,
    tz: TZ,
    tasks: {
      rows: [...rows.values()].map(({ id, fields, version, created, updated }): Task => ({ id, fields, version, created, updated })),
      rev: revOf("tasks"),
      space: "home",
      goals: GOALS,
      missing: false,
      blocks: near.map(({ id, fields, version }) => ({ id, fields, version }) as Block),
    },
    calendar: calendar(),
    reminders: items.flatMap((i) => (i.kind === "reminder" ? [{ id: i.id, title: i.title, at: i.since, when: i.when ?? null, repeat: i.repeat ?? null }] : [])),
    plan: {
      at: t,
      plans,
      recent: recent.slice(0, 20),
      holds: holds.filter((h) => h.to > t).map(({ from: f, to: e, label }) => ({ from: f, to: e, label })),
      activities,
      meals: meals(t),
      said,
      window,
    },
  };
  return { schema: SCHEMA, ...plan };
}

// --- `POST /api/plan/press` ----------------------------------------------------------------------------------------

const HM = /^[0-2]\d:[0-5]\d$/;
const ID = /^[0-9A-HJKMNP-TV-Z]{26}$/;
const SHAPES: Record<string, RegExp> = {
  day: /^\d{4}-\d{2}-\d{2}$/,
  task: ID,
  block: ID,
  plan: /^p[0-9a-f]{10}$/,
  t: /^[0-9a-f]{32}$/,
  ref: /^reminder:\d{1,12}$/,
  opens: HM,
  closes: HM,
  zone: /^[A-Za-z][A-Za-z0-9_+-]{0,30}(?:\/[A-Za-z0-9_+-]{1,30}){0,2}$/,
};

/** A press as retinue/plan.py's `clean_press` takes it — its action, and only the fields that action takes, each of
 * its shape — or the words of its refusal. */
export function cleanPress(body: unknown): Press | string {
  const given = (body && typeof body === "object" ? body : {}) as Fields;
  const action = given.action as Press["action"];
  if (!PRESSES.includes(action)) return `action: one of ${PRESSES.join(", ")}`;
  let takes = PRESS_FIELDS[action];
  if (action === "place") takes = given.task != null ? ["day", "task"] : given.t != null ? ["day", "t"] : ["day", "ref"];
  const out: Fields = { action };
  for (const key of takes) {
    const value = given[key];
    if (key === "activity") {
      if (typeof value !== "string" || !(value.length > 0 && value.length <= 40)) return "activity: its name";
    } else if (key === "activities") {
      const ok = Array.isArray(value) && value.length <= 12 && value.every((a: Fields) =>
        !!a && typeof a === "object" && Object.keys(a).every((k) => ["title", "minutes", "from", "to"].includes(k)) &&
        typeof a.title === "string" && a.title.length <= 40 && Number.isInteger(a.minutes) &&
        ["from", "to"].every((k) => a[k] == null || (typeof a[k] === "string" && HM.test(a[k]))));
      if (!ok) return "activities: up to 12 of {title, minutes, from?, to?}";
    } else if (key === "minutes") {
      if (value !== 30 && value !== 60 && value !== 120) return "minutes: 30, 60 or 120";
    } else if (key === "days") {
      if (value == null) continue;
      if (value !== 1 && value !== 7) return "days: 1 or 7";
    } else if (value == null && key === "day" && action === "plan") {
      continue;
    } else if (typeof value !== "string" || !SHAPES[key]?.test(value)) {
      return `${key}: not of its shape`;
    }
    out[key] = value;
  }
  return out as Press;
}

/** «Plan» on a person's row or a reminder: its task, made as the Router makes it — or the open one there already. */
function makeTask(by: { t: string } | { ref: string }): string | null {
  const item = digest().find((i) => ("t" in by ? i.t === by.t : i.id === by.ref));
  if (!item) return null;
  const there = recordsOf("tasks").find((t) => t.fields.ask === item.id && t.fields.state === "open");
  if (there) return there.id;
  const fields = { title: item.title, state: "open", ask: item.id, ...(item.when ? { due: item.when } : {}) };
  return routerWrite("tasks", [{ op: "add", fields }], "code:waiting")[0]?.id ?? null;
}

/** The blocks a plan wrote, kept under its id for its «Undo», each with what it was before. */
function keep(id: string, records: CollectionRecord[], before: Map<string, Fields> = new Map()) {
  const mine = written.get(id) ?? new Map();
  for (const r of records) mine.set(r.id, { version: r.version, before: mine.get(r.id)?.before ?? before.get(r.id) ?? null });
  written.set(id, mine);
}
const fieldsBefore = (b: CollectionRecord): Fields => ({ start: b.fields.start, minutes: b.fields.minutes, state: b.fields.state, plan: b.fields.plan ?? null });

/** «Plan my day», «Plan my week», or one task's «Plan» to a day: two open tasks with nothing planned a day, in a row
 * from 19:00 on the window's clock, under one plan id. */
function plan(press: Extract<Press, { action: "plan" | "place" }>, t: number): string {
  let only: string | null = null;
  if (press.action === "place") {
    only = "task" in press ? press.task : makeTask("t" in press ? { t: press.t } : { ref: press.ref });
    if (!only) return "Not planned: that row is not in the list any more. Look again.";
  }
  const days = press.action === "plan" && press.days === 7 ? 7 : 1;
  const id = pid();
  const first = press.day ?? windowDay(t);
  const planned = new Set(blocks().filter((b) => b.fields.state === "planned").map((b) => b.fields.task));
  let made = 0;
  for (let n = 0; n < days; n++) {
    const d = addDays(first, n);
    const end = windowOf(d)[1];
    let at = Math.max(wallAt(d, 19 * 60, window.zone), Math.ceil(t / 900) * 900);
    const ops: Op[] = [];
    for (const task of recordsOf("tasks")) {
      if (task.fields.state !== "open" || planned.has(task.id) || (only && task.id !== only)) continue;
      const minutes = Math.min(Number(task.fields.minutes ?? 30), 90);
      if (at + minutes * 60 > end) break;
      ops.push({ op: "add", fields: { task: task.id, start: iso(at), minutes, state: "planned", plan: id } });
      planned.add(task.id);
      at += minutes * 60 + 900;
      if (ops.length === 2) break;
    }
    const laid = routerWrite("blocks", ops);
    keep(id, laid);
    made += laid.length;
    // What did not fit this window: the open tasks still with nothing planned, as the Router names them.
    const left = recordsOf("tasks")
      .filter((x) => x.fields.state === "open" && !planned.has(x.id) && !only && days === 1)
      .map((x) => ({ task: x.id, title: String(x.fields.title), why: "no free 30 min before the window ends" }));
    plans[d] = { id, at: t, blocks: laid.map((b) => b.id), left, undone: false, failed: false };
  }
  recent.unshift({ id, by: "owner", at: t, undone: false, failed: false });
  return days === 7 ? `Week planned: ${made} blocks.` : `Plan for ${first}: ${made} ${made === 1 ? "task" : "tasks"}.`;
}

/** «Undo» of one plan: its blocks still as it left them go back — taken off when it added them; those changed since
 * are the owner's and are named. «Not working today» taken back takes its hold off too. */
function undo(id: string): string {
  const mine = written.get(id);
  const entry = recent.find((p) => p.id === id);
  if (!mine || !entry || entry.undone) return "That plan is not known any more: nothing taken back.";
  let kept = 0;
  const ops: Op[] = [];
  for (const b of blocks()) {
    const known = mine.get(b.id);
    if (!known || b.fields.state !== "planned") continue;
    if (b.version !== known.version) kept += 1;
    else ops.push({ op: "set", id: b.id, version: b.version, fields: known.before ?? { state: "skipped" } });
  }
  const back = routerWrite("blocks", ops);
  for (const p of Object.values(plans)) if (p.id === id) p.undone = true;
  entry.undone = true;
  written.delete(id);
  holds = holds.filter((h) => h.plan !== id);
  return `Taken back: ${back.length} ${back.length === 1 ? "block" : "blocks"}.${kept ? ` ${kept} you changed since stay.` : ""}`;
}

/** A hold from `start` to `end`: one running at `start` ends there, one that would start inside it is taken off and
 * named, and the plan's blocks it crosses — not his own — are laid again after it. */
function hold(label: string, start: number, end: number): string {
  const dropped = holds.filter((h) => start <= h.from && h.from < end);
  holds = [...holds.filter((h) => !dropped.includes(h)).map((h) => (h.from < start && start < h.to ? { ...h, to: start } : h)), { from: start, to: end, label }]
    .sort((a, b) => a.from - b.from);
  let at = end;
  const crossed = blocks()
    .filter((b) => b.fields.state === "planned" && !b.fields.locked && spanOf(b)[0] < end && spanOf(b)[1] > start)
    .sort((a, b) => spanOf(a)[0] - spanOf(b)[0]);
  const moved = routerWrite("blocks", crossed.map((b): Op => {
    const op: Op = { op: "set", id: b.id, version: b.version, fields: { start: iso(at) } };
    at += Number(b.fields.minutes) * 60;
    return op;
  }));
  const gone = dropped.length ? ` Taken off: ${dropped.map((h) => h.label).join(", ")}.` : "";
  return `${label} till ${hmIn(end, window.zone)}.${gone}${moved.length ? ` Moved ${moved.length} after it.` : ""}`;
}

/** «Not working today»: a hold «Not working» from now to the end of today's window, and the plan's blocks in it that
 * are not his — the one running cut at now, those ahead moved a day on — as a plan of its own: one «Undo» puts every
 * block back and takes the hold off. A second press while it holds is the first one. */
function off(t: number): string {
  let [opens, end] = windowOf(windowDay(t));
  if (t >= end) [opens, end] = windowOf(dayIn(t, window.zone));
  const start = Math.max(t, opens);
  if (holds.some((h) => h.plan && h.label === "Not working" && h.to >= end)) return `Not working till ${hmIn(end, window.zone)} already.`;
  const id = pid();
  const ahead = blocks().filter((b) => b.fields.state === "planned" && spanOf(b)[1] > start && spanOf(b)[0] < end);
  const mine = ahead.filter((b) => !b.fields.locked);
  const his = ahead.filter((b) => b.fields.locked);
  const running = mine.filter((b) => spanOf(b)[0] < start);
  const ops = mine.map((b): Op =>
    running.includes(b)
      ? { op: "set", id: b.id, version: b.version, fields: { minutes: Math.max(5, Math.ceil((start - spanOf(b)[0]) / 60)), plan: id } }
      : { op: "set", id: b.id, version: b.version, fields: { start: iso(spanOf(b)[0] + 86400), plan: id } },
  );
  keep(id, routerWrite("blocks", ops), new Map(mine.map((b) => [b.id, fieldsBefore(b)])));
  holds = [...holds, { from: start, to: end, label: "Not working", plan: id }].sort((a, b) => a.from - b.from);
  recent.unshift({ id, by: "owner", at: t, undone: false, failed: false });
  const moved = mine.length - running.length;
  const title = (b: CollectionRecord) => String(recordOf("tasks", String(b.fields.task))?.fields.title ?? "");
  return `Not working till ${hmIn(end, window.zone)}.${moved ? ` Moved ${moved} ${moved === 1 ? "block" : "blocks"} a day on.` : ""}${his.length ? ` Yours stay: ${his.map(title).join(", ")}.` : ""}`;
}

const isZone = (zone: string) => {
  try {
    new Intl.DateTimeFormat("en", { timeZone: zone });
    return true;
  } catch {
    return false;
  }
};

/** What a press does, in the Router's words (`Planner._pressed`). */
function pressed(press: Press, t: number): string {
  switch (press.action) {
    case "plan":
    case "place":
      return plan(press, t);
    case "undo":
      return undo(press.plan);
    case "busy":
      return hold("Busy", t, t + press.minutes * 60);
    case "now": {
      const a = activities.find((x) => x.title.toLowerCase() === press.activity.toLowerCase());
      return a ? hold(a.title, t, t + a.minutes * 60) : "No such activity — it was taken off the list. Look again.";
    }
    case "free": {
      const running = holds.filter((h) => h.from <= t && t < h.to);
      holds = holds.map((h) => (running.includes(h) ? { ...h, to: t } : h));
      return running[0] ? `${running[0].label} taken off.` : "Nothing is running.";
    }
    case "remove": {
      const b = blocks().find((x) => x.id === press.block && x.fields.state === "planned");
      if (!b) return "That block is not planned any more.";
      return routerWrite("blocks", [{ op: "set", id: b.id, version: b.version, fields: { state: "skipped" } }]).length ? "Taken off the day." : "Not taken off: try again.";
    }
    case "hours": {
      if ((minutesOf(press.closes) - minutesOf(press.opens) + 1440) % 1440 < 120 || !isZone(press.zone))
        return "Not kept: a window is two times at least two hours apart and a zone.";
      window = { zone: press.zone, opens: press.opens, closes: press.closes };
      return `Plan window: ${press.opens}–${press.closes}, ${press.zone}. The next plan uses it.`;
    }
    case "activities": {
      const given = press.activities;
      const names = new Set(given.map((a) => a.title.trim().toLowerCase()));
      if (names.size < given.length || given.some((a) => !a.title.trim() || a.minutes < 5 || a.minutes > 720 || !a.from !== !a.to))
        return "Not kept: up to 12 activities, each named once, 5 min to 12 h, a range both ends.";
      activities = given.map((a) => ({ title: a.title.trim(), minutes: a.minutes, ...(a.from && a.to ? { from: a.from, to: a.to } : {}) }));
      const kept = activities.filter((a) => a.from).map((a) => a.title);
      return `Activities kept: ${activities.length}${kept.length ? `, meals keep their place: ${kept.join(", ")}.` : "."}`;
    }
    case "off":
      return off(t);
  }
}

/** A press checked already, done at once here as the Router does it a moment later: what it said — kept as the plan's
 * `said` — and the collections' events of its writes. */
export function mockPlanPress(press: Press): { said: string; moved: Moved[] } {
  const t = now();
  told.length = 0;
  const text = pressed(press, t);
  const d = press.action === "plan" || press.action === "place" ? (press.day ?? windowDay(t)) : null;
  said = { at: t, text, ...(d ? { day: d } : {}) };
  return { said: text, moved: [...told] };
}

/** The assistant's ask done, as the Router's Planner does it: the report's block to tomorrow evening and two hours long,
 * one plan with its «before» — `POST /api/_mock/assistant`. Its «Undo» is the plan's, as a press of the owner's. */
export function mockAssistantMove(): { plan: string; moved: Moved[] } {
  const t = now();
  told.length = 0;
  const b = blocks().find((x) => x.fields.task === report.id && x.fields.state === "planned");
  if (!b) return { plan: "", moved: [] };
  const id = pid();
  const from = String(b.fields.start);
  const moved = routerWrite("blocks", [{ op: "set", id: b.id, version: b.version, fields: { start: iso(wall(1, 20, 0)), minutes: 120, plan: id } }]);
  keep(id, moved, new Map([[b.id, fieldsBefore(b)]]));
  recent.unshift({ id, by: "assistant", at: t, undone: false, failed: false });
  said = { at: t, text: `The assistant moved «${report.fields.title}» from ${from.slice(0, 16)} to tomorrow 20:00, 2 h.` };
  return { plan: id, moved: [...told] };
}
