// «Today»'s tasks, day and week (docs/face.md «Tasks, Day, Week»): one read of the platform (`GET /api/plan`) — the
// open tasks and those with a slot near, the calendar's busy window as the Router last pushed it, the digest's asks
// and reminders — and the words and the arithmetic of the three tabs: a day in the owner's zone, a slot's place in
// it, the first free time of an evening. Writes go through the collections' own route, as any screen's do.
import { useCallback, useEffect, useRef, useState } from "react";
import { request } from "./api";
import { live as theLive, type Live } from "./live";

export interface TaskFields {
  title?: string;
  goal?: string;
  state?: "open" | "done" | "dropped";
  due?: string;
  minutes?: number;
  slot?: string;
  locked?: boolean;
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

export interface Plan {
  now: number;
  tz: string;
  tasks: { rows: Task[]; rev: string | null; space: string | null; goals: { value: string; label: string }[]; missing: boolean };
  calendar: { events: CalEvent[]; at: number | null; failed: number; stale: boolean };
  asks: PlanAsk[];
  reminders: PlanReminder[];
}

export const planApi = {
  get: () => request<Plan>("/api/plan"),
  /** «Make it a task» on a person's row (its token) or a reminder (its id): the Router makes it, and the next read has it. */
  task: (by: { t: string } | { ref: string }) => request<{ ok: boolean }>("/api/waiting/press", { ...by, action: "task" }),
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
    const off = live.onCollection((event) => event.name === "tasks" && reload());
    return () => {
      clearInterval(timer);
      off();
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

/** A slot as kept: a moment in UTC, `YYYY-MM-DDTHH:MM:SSZ`. */
export function iso(ts: number): string {
  return new Date(Math.round(ts) * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
}

export function slotOf(task: Task): number | null {
  const s = task.fields.slot;
  if (!s) return null;
  const t = Date.parse(s);
  return Number.isFinite(t) ? t / 1000 : null;
}

export const DEFAULT_MINUTES = 30;
export const minutesOf = (task: Task) => task.fields.minutes && task.fields.minutes > 0 ? task.fields.minutes : DEFAULT_MINUTES;

/** The hours a day's tasks go in when none is named: an evening on a weekday, the day on a weekend. */
export function hours(day: string): [number, number] {
  const wd = weekday(day);
  return wd === 0 || wd === 6 ? [10 * 60, 23 * 60] : [17 * 60, 23 * 60];
}

const STEP = 15;

/** The first free start of `minutes` on `day` inside its window, at a quarter hour, not before `now`, clear of what
 * keeps him busy and of the slots already there; past the window's end when nothing is free — the latest start that
 * still ends by midnight. */
export function firstFree(day: string, minutes: number, taken: [number, number][], now: number, tz: string): number {
  const [from, to] = hours(day);
  for (let m = from; m + minutes <= to; m += STEP) {
    const start = at(day, m, tz);
    const end = start + minutes * 60;
    if (start < now) continue;
    if (taken.every(([a, b]) => end <= a || start >= b)) return start;
  }
  return at(day, Math.max(from, 24 * 60 - minutes), tz);
}

/** What keeps him busy and what is planned already, as moments: the calendar's busy occurrences and the open tasks'
 * slots, the one being moved aside. */
export function taken(plan: Plan, except?: string): [number, number][] {
  const out: [number, number][] = plan.calendar.events.filter((e) => e.busy).map((e) => [e.start, e.end]);
  for (const t of plan.tasks.rows) {
    const s = slotOf(t);
    if (s !== null && t.id !== except && t.fields.state === "open") out.push([s, s + minutesOf(t) * 60]);
  }
  return out;
}

/** A goal's swatch, by its place in the form — a series' colour, never a meaning of its own. */
export function goalSwatch(goals: Plan["tasks"]["goals"], goal: string | undefined): string {
  const n = goals.findIndex((g) => g.value === goal);
  return n < 0 ? "bg-muted-foreground/40" : (["bg-chart-1", "bg-chart-2", "bg-chart-3", "bg-chart-4", "bg-chart-5"][n % 5] ?? "");
}

export function goalLabel(goals: Plan["tasks"]["goals"], goal: string | undefined): string | null {
  return goals.find((g) => g.value === goal)?.label ?? null;
}

/** The asks and reminders a task already names: they need no «Make it a task» again. */
export function named(plan: Plan): Set<string> {
  return new Set(plan.tasks.rows.filter((t) => t.fields.state === "open" && t.fields.ask).map((t) => t.fields.ask as string));
}
