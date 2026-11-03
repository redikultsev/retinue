// «Today»'s tasks, day and week (docs/face.md «Tasks, Day, Week»): one read of the platform (`GET /api/plan`) — the
// open tasks, their blocks of time near, the calendar's busy window and the day plan's state as the Router last pushed
// them, the digest's asks and reminders — and the words and the arithmetic of the three tabs. A day is a window of the
// plan's: its hours in its zone, as the owner set them on the plan's settings, shown in the owner's own. Plans are pressed (`/api/plan/press`) and
// laid by the Router; the owner's own moves of a block go through the collections' route, as any screen's do.
import { useCallback, useEffect, useRef, useState } from "react";
import { request } from "./api";
import { live as theLive, type Live } from "./live";

export interface TaskFields {
  title?: string;
  goal?: string;
  /** The project it belongs to: its path in the Base (`work/acme`); absent: none. */
  project?: string;
  state?: "open" | "done" | "dropped";
  due?: string;
  /** «No later than»: no piece of it is laid to end after this moment. */
  by?: string;
  minutes?: number;
  ask?: string;
  notes?: string;
}

export interface Task {
  id: string;
  fields: TaskFields;
  version: number;
  created: number;
  updated: number;
}

export interface CalEvent {
  start: number;
  end: number;
  title: string;
  all_day: boolean;
  busy: boolean;
}

export interface PlanAsk {
  id: string;
  kind: "letter" | "chat";
  who: string;
  title: string;
  since: number;
  t?: string;
}

export interface PlanReminder {
  id: string;
  title: string;
  at: number;
  when: string | null;
  repeat: string | null;
}

export interface Block {
  id: string;
  fields: { task: string; start: string; minutes: number; state: "planned" | "passed" | "skipped"; locked?: boolean; plan?: string; why?: string };
  version: number;
}

export interface DayPlan {
  id: string;
  at: number;
  left: { task: string; title: string; why: string }[];
  /** What the plan could not fit before a task's deadline in this window: minutes still missing. */
  short?: { task: string; title: string; minutes: number }[];
  blocks: string[];
  undone: boolean;
  failed: boolean;
}

export interface RecentPlan {
  id: string;
  by: "owner" | "assistant";
  at: number;
  undone: boolean;
  failed: boolean;
}

/** Something of the owner's that is not a task — a meal, the road, rest: a hold of its name on «Now»; with a range of
 * starts (`from`–`to`, wall times in the plan's zone) a meal, whose place the plan keeps free each day. */
export interface Activity {
  title: string;
  minutes: number;
  from?: string;
  to?: string;
}

export interface PlanWindow {
  zone: string;
  opens: string;
  closes: string;
}

export interface Plan {
  now: number;
  tz: string;
  tasks: {
    rows: Task[];
    rev: string | null;
    space: string | null;
    goals: { value: string; label: string }[];
    missing: boolean;
    blocks: Block[];
  };
  calendar: { events: CalEvent[]; at: number | null; failed: number; stale: boolean };
  asks: PlanAsk[];
  reminders: PlanReminder[];
  plan: {
    at: number | null;
    plans: Record<string, DayPlan>;
    /** The plans of the last day, newest first: who asked for each — her plans' blocks are lit on the grid. */
    recent?: RecentPlan[];
    holds: { from: number; to: number; label: string }[];
    /** His activities, in his order: the «Now» menu, and the meals. */
    activities?: Activity[];
    /** The places the meals keep, from yesterday's window a week on. */
    meals?: { title: string; from: number; to: number }[];
    said: { at?: number; text?: string; day?: string };
    window: PlanWindow;
  };
}

export type Press =
  | { action: "plan"; day?: string; days?: 1 | 7 }
  | { action: "place"; day: string; task: string }
  /** «Plan» on a person's row (its token) or a reminder (its id): the Router makes its task and lays it. */
  | { action: "place"; day: string; t: string }
  | { action: "place"; day: string; ref: string }
  | { action: "undo"; plan: string }
  | { action: "busy"; minutes: 30 | 60 | 120 }
  | { action: "free" }
  | { action: "remove"; block: string }
  /** The plan's window, set on its settings: two wall times in a zone, a close before the open after midnight. */
  | ({ action: "hours" } & PlanWindow)
  /** An activity from now: a hold of its name and length. */
  | { action: "now"; activity: string }
  | { action: "activities"; activities: Activity[] }
  /** «Not working today»: the rest of today's window held, its blocks moved to the next days — one «Undo». */
  | { action: "off" };

export const planApi = {
  get: () => request<Plan>("/api/plan"),
  /** A press on the plan: the Router lays it, and the next read — at once, by the stream — has it. */
  press: (body: Press) => request<{ ok: boolean }>("/api/plan/press", body),
};

const REFRESH_MS = 60_000;

/** The plan, read now, again once a minute (the calendar moves every ten), and at once when `tasks` changes. */
export function usePlan(live: Live = theLive, api = planApi): { plan: Plan | null; error: string | null; reload: () => void } {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  const reload = useCallback(() => {
    const n = ++seq.current;
    api.get().then(
      (p) => {
        if (n !== seq.current) return;
        setPlan(p);
        setError(null);
      },
      (e: unknown) => n === seq.current && setError(e instanceof Error ? e.message : "Not loaded."),
    );
  }, [api]);
  useEffect(() => {
    reload();
    const timer = setInterval(reload, REFRESH_MS);
    const off = live.onCollection((event) => (event.name === "tasks" || event.name === "blocks") && reload());
    const offPlan = live.onPlan(reload);
    return () => {
      clearInterval(timer);
      off();
      offPlan();
    };
  }, [live, reload]);
  return { plan, error, reload };
}

// --- time in the owner's zone ------------------------------------------------------------------------------------

const PARTS = new Map<string, Intl.DateTimeFormat>();

function parts(ts: number, tz: string): { y: number; m: number; d: number; h: number; min: number; wd: number } {
  let f = PARTS.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "numeric",
      day: "numeric", hour: "numeric", minute: "numeric", weekday: "short" });
    PARTS.set(tz, f);
  }
  const got: Record<string, string> = {};
  for (const p of f.formatToParts(new Date(ts * 1000))) got[p.type] = p.value;
  const wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(got.weekday ?? "");
  return { y: Number(got.year), m: Number(got.month), d: Number(got.day), h: Number(got.hour) % 24, min: Number(got.minute), wd };
}

const pad = (n: number) => String(n).padStart(2, "0");

/** A day's year, month and day as numbers. */
export function ymd(day: string): [number, number, number] {
  const [y = 1970, m = 1, d = 1] = day.split("-").map(Number);
  return [y, m, d];
}

/** The owner's day a moment falls on: `YYYY-MM-DD`. */
export function dayOf(ts: number, tz: string): string {
  const p = parts(ts, tz);
  return `${p.y}-${pad(p.m)}-${pad(p.d)}`;
}

/** A moment's wall time in his zone: «18:30». */
export function hm(ts: number, tz: string): string {
  const p = parts(ts, tz);
  return `${pad(p.h)}:${pad(p.min)}`;
}

/** The moment a wall time of his day is, in his zone (across a change of offset, the later reading). */
export function at(day: string, minutes: number, tz: string): number {
  const [y, m, d] = ymd(day);
  const wall = Date.UTC(y, m - 1, d, Math.floor(minutes / 60), minutes % 60) / 1000;
  let guess = wall;
  for (let i = 0; i < 3; i++) {
    const p = parts(guess, tz);
    const seen = Date.UTC(p.y, p.m - 1, p.d, p.h, p.min) / 1000;
    guess += wall - seen;
  }
  return guess;
}

/** The day `n` days after `day`. */
export function addDays(day: string, n: number): string {
  const [y, m, d] = ymd(day);
  const t = new Date(Date.UTC(y, m - 1, d + n));
  return `${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())}`;
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** A day's weekday, 0 for Sunday. */
export function weekday(day: string): number {
  const [y, m, d] = ymd(day);
  return new Date(Date.UTC(y, m - 1, d)).getUTCDay();
}

/** A day as the tabs say it: «Today», «Tomorrow», «Wed 4 Nov». */
export function dayWords(day: string, today: string, short = false): string {
  if (day === today) return "Today";
  if (day === addDays(today, 1)) return "Tomorrow";
  if (day === addDays(today, -1)) return "Yesterday";
  const [, m, d] = ymd(day);
  return short ? `${WEEKDAYS[weekday(day)]} ${d}` : `${WEEKDAYS[weekday(day)]} ${d} ${MONTHS[m - 1] ?? ""}`;
}

/** A length of time: «45 min», «1 h 30», «2 h». */
export function duration(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${h} h ${pad(rest)}` : `${h} h`;
}

/** A moment as the collections keep it: in UTC, `YYYY-MM-DDTHH:MM:SSZ`. */
export function iso(ts: number): string {
  return new Date(Math.round(ts) * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
}

export const moment = (value: string | undefined): number | null => {
  const t = value ? Date.parse(value) : NaN;
  return Number.isFinite(t) ? t / 1000 : null;
};

export const DEFAULT_MINUTES = 30;
export const minutesOf = (task: Task) => (task.fields.minutes && task.fields.minutes > 0 ? task.fields.minutes : DEFAULT_MINUTES);

const minutesOfDay = (hhmm: string) => {
  const [h = 0, m = 0] = hhmm.split(":").map(Number);
  return h * 60 + m;
};

/** The plan's window of a day: from its opening to its closing — the next day when that is earlier — in its zone. */
export function windowOf(day: string, w: PlanWindow): [number, number] {
  const opens = minutesOfDay(w.opens);
  const closes = minutesOfDay(w.closes);
  return [at(day, opens, w.zone), at(closes <= opens ? addDays(day, 1) : day, closes, w.zone)];
}

/** The window a moment belongs to: one past midnight belongs to the evening before. */
export function windowDay(ts: number, w: PlanWindow): string {
  const day = dayOf(ts, w.zone);
  const closes = minutesOfDay(w.closes);
  const pastMidnight = closes <= minutesOfDay(w.opens) && minutesOfDay(hm(ts, w.zone)) < closes;
  return pastMidnight ? addDays(day, -1) : day;
}

/** The window a plan made now is for: the one we are in while an hour of it is left, else the next. */
export function planDay(now: number, w: PlanWindow): string {
  const day = windowDay(now, w);
  return now < windowOf(day, w)[1] - 3600 ? day : addDays(day, 1);
}

export interface Placed {
  block: Block;
  task: Task | undefined;
  start: number;
  end: number;
}

/** The blocks still planned or passed, each with its task and its moments. */
export function placed(plan: Plan): Placed[] {
  const tasks = new Map(plan.tasks.rows.map((t) => [t.id, t]));
  return plan.tasks.blocks
    .filter((b) => b.fields.state !== "skipped")
    .map((b) => {
      const start = moment(b.fields.start) ?? 0;
      return { block: b, task: tasks.get(b.fields.task), start, end: start + b.fields.minutes * 60 };
    })
    .filter((p) => !p.task || p.task.fields.state === "open" || p.block.fields.state === "passed");
}

/** A task's next planned block, if it has one ahead. */
export function nextBlock(plan: Plan, task: Task): Placed | undefined {
  return placed(plan)
    .filter((p) => p.task?.id === task.id && p.block.fields.state === "planned" && p.end > plan.now)
    .sort((a, b) => a.start - b.start)[0];
}

/** What its passed blocks counted of a task, in minutes. */
export function doneOf(plan: Plan, task: Task): number {
  return plan.tasks.blocks
    .filter((b) => b.fields.task === task.id && b.fields.state === "passed")
    .reduce((n, b) => n + b.fields.minutes, 0);
}

/** A goal's swatch, by its place in the form — a series' colour, never a meaning of its own. */
export function goalSwatch(goals: Plan["tasks"]["goals"], goal: string | undefined): string {
  const n = goals.findIndex((g) => g.value === goal);
  return n < 0 ? "bg-muted-foreground/40" : (["bg-chart-1", "bg-chart-2", "bg-chart-3", "bg-chart-4", "bg-chart-5"][n % 5] ?? "");
}

export function goalLabel(goals: Plan["tasks"]["goals"], goal: string | undefined): string | null {
  return goals.find((g) => g.value === goal)?.label ?? null;
}

/** The asks and reminders a task already names: «From people» shows them no more. */
export function named(plan: Plan): Set<string> {
  return new Set(plan.tasks.rows.filter((t) => t.fields.state === "open" && t.fields.ask).map((t) => t.fields.ask as string));
}

/** What the plans from `today` on say is short before the deadline, by task: its minutes still missing. */
export function shorts(plan: Plan, today: string): Map<string, { title: string; minutes: number }> {
  const out = new Map<string, { title: string; minutes: number }>();
  for (const [day, p] of Object.entries(plan.plan.plans)) {
    if (day < today || p.undone) continue;
    for (const x of p.short ?? []) {
      const open = plan.tasks.rows.some((t) => t.id === x.task && t.fields.state === "open");
      if (open && (out.get(x.task)?.minutes ?? 0) < x.minutes) out.set(x.task, { title: x.title, minutes: x.minutes });
    }
  }
  return out;
}
