// Day: one day as it will go — a strip of the week to pick it, how much is planned of the free time in its hours and
// on what, then the day in order: the calendar's meetings, the tasks in their slots, the reminders at their time, the
// free stretches of its hours. A task dragged onto a free stretch starts there, and stays where the owner put it.
import { useMemo, useState } from "react";
import { useEditing } from "@/components/views/edit";
import { Bell } from "@/components/icons";
import {
  addDays,
  at,
  dayOf,
  dayWords,
  duration,
  goalLabel,
  goalSwatch,
  hm,
  hours,
  iso,
  minutesOf,
  type CalEvent,
  type Plan,
  type PlanReminder,
  slotOf,
  type Task,
  weekday,
  ymd,
} from "@/lib/plan";
import { cn, LIST } from "@/lib/utils";
import { dragProps, PlanMenu, TaskCheck, TaskMeta, useDrop, useSetTask } from "./shared";
import { NeedsYou } from "./tasks-tab";

const STRIP_DAYS = 7;
const WEEKDAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const GAP_MIN = 15;

type Item =
  | { kind: "event"; start: number; end: number; event: CalEvent }
  | { kind: "task"; start: number; end: number; task: Task }
  | { kind: "reminder"; start: number; end: number; reminder: PlanReminder }
  | { kind: "free"; start: number; end: number };

/** What a day holds, in its order, with the free stretches of its hours between — none in the past. */
export function dayItems(plan: Plan, day: string): Item[] {
  const tz = plan.tz;
  const items: Item[] = [];
  for (const e of plan.calendar.events)
    if (!e.all_day && dayOf(e.start, tz) === day) items.push({ kind: "event", start: e.start, end: e.end, event: e });
  for (const t of plan.tasks.rows) {
    const s = slotOf(t);
    if (s !== null && t.fields.state !== "dropped" && dayOf(s, tz) === day)
      items.push({ kind: "task", start: s, end: s + minutesOf(t) * 60, task: t });
  }
  for (const r of plan.reminders) if (dayOf(r.at, tz) === day) items.push({ kind: "reminder", start: r.at, end: r.at, reminder: r });
  const [from, to] = hours(day);
  const busy = items
    .filter((i) => (i.kind === "event" && i.event.busy) || (i.kind === "task" && i.task.fields.state === "open"))
    .map((i) => [i.start, i.end] as [number, number])
    .sort((a, b) => a[0] - b[0]);
  let cursor = Math.max(at(day, from, tz), Math.ceil(plan.now / 900) * 900);
  const end = at(day, to, tz);
  for (const [a, b] of busy) {
    if (a - cursor >= GAP_MIN * 60 && cursor < end) items.push({ kind: "free", start: cursor, end: Math.min(a, end) });
    cursor = Math.max(cursor, b);
  }
  if (end - cursor >= GAP_MIN * 60) items.push({ kind: "free", start: cursor, end });
  return items.sort((x, y) => x.start - y.start || (x.kind === "free" ? 1 : 0) - (y.kind === "free" ? 1 : 0));
}

/** How much of a day's hours is free, and how much of it is planned already, in minutes. */
export function load(plan: Plan, day: string): { planned: number; free: number } {
  let planned = 0;
  let free = 0;
  for (const i of dayItems(plan, day)) {
    if (i.kind === "task" && i.task.fields.state === "open") planned += Math.round((i.end - i.start) / 60);
    if (i.kind === "free") free += Math.round((i.end - i.start) / 60);
  }
  return { planned, free };
}

function Strip({ plan, today, day, onPick }: { plan: Plan; today: string; day: string; onPick: (d: string) => void }) {
  return (
    <div role="tablist" aria-label="Days" className="grid grid-cols-7 gap-1.5 max-md:flex max-md:overflow-x-auto max-md:pb-1">
      {Array.from({ length: STRIP_DAYS }, (_, n) => addDays(today, n)).map((d) => {
        const { planned, free } = load(plan, d);
        const [, , dd] = ymd(d);
        return (
          <button
            key={d}
            type="button"
            role="tab"
            aria-selected={d === day}
            onClick={() => onPick(d)}
            title={planned ? `${duration(planned)} planned, ${duration(free)} free` : `${duration(free)} free`}
            className={cn(
              "flex min-w-0 flex-col items-start gap-0.5 rounded-lg border px-2 py-1.5 text-left transition-colors max-md:min-w-[4.5rem]",
              d === day ? "border-foreground/40 bg-accent" : "hover:bg-accent/50",
            )}
          >
            <span className="flex max-w-full items-baseline gap-1 truncate text-xs text-muted-foreground">
              {d === today ? "Today" : WEEKDAY[weekday(d)]}
              <span className="font-mono text-[13px] font-medium text-foreground tabular-nums">{dd}</span>
            </span>
            <span className={cn("max-w-full truncate font-mono text-xs tabular-nums", planned ? "text-warning" : "text-muted-foreground")}>
              {planned ? duration(planned) : free ? duration(free) : "full"}
            </span>
          </button>
        );
      })}
    </div>
  );
}

function Summary({ plan, day }: { plan: Plan; day: string }) {
  const { planned, free } = load(plan, day);
  const [from, to] = hours(day);
  const byGoal = new Map<string, number>();
  for (const i of dayItems(plan, day))
    if (i.kind === "task" && i.task.fields.state === "open") {
      const g = i.task.fields.goal ?? "";
      byGoal.set(g, (byGoal.get(g) ?? 0) + Math.round((i.end - i.start) / 60));
    }
  const total = planned + free;
  const pad = (m: number) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  return (
    <div className="flex flex-col gap-3 rounded-xl border bg-card px-4 py-3">
      <p className="flex flex-wrap items-baseline gap-x-2">
        <span className="text-xl font-medium tabular-nums">{duration(planned)}</span>
        <span className="text-sm text-muted-foreground">
          planned of {duration(total)} free in your hours, {pad(from)}–{pad(to)}
        </span>
      </p>
      {total > 0 && (
        <div className="flex h-2 overflow-hidden rounded-full bg-muted" aria-hidden>
          {[...byGoal].map(([g, m]) => (
            <span key={g} className={cn("h-full border-r-2 border-card last:border-r-0", goalSwatch(plan.tasks.goals, g))} style={{ width: `${(m / total) * 100}%` }} />
          ))}
        </div>
      )}
      {byGoal.size > 0 && (
        <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          {[...byGoal].map(([g, m]) => (
            <li key={g} className="inline-flex items-center gap-1.5">
              <span aria-hidden className={cn("size-2 rounded-[2px]", goalSwatch(plan.tasks.goals, g))} />
              {goalLabel(plan.tasks.goals, g) ?? "No goal"}
              <span className="font-mono text-foreground tabular-nums">{duration(m)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Time({ start, end }: { start: string; end?: string }) {
  return (
    <span className="flex w-12 shrink-0 flex-col font-mono text-xs leading-5 text-muted-foreground tabular-nums">
      <span className="text-foreground">{start}</span>
      {end !== undefined && <span>{end}</span>}
    </span>
  );
}

function FreeRow({ plan, item, today }: { plan: Plan; item: Item; today: string }) {
  const set = useSetTask();
  const drop = useDrop(plan, (task) =>
    void set(task, { slot: iso(item.start), locked: true }, `Planned: ${dayWords(dayOf(item.start, plan.tz), today)}, ${hm(item.start, plan.tz)}.`),
  );
  return (
    <li
      {...drop.props}
      className={cn("flex min-h-10 items-center gap-3 px-3 py-1.5 text-xs text-muted-foreground transition-colors", drop.over && "bg-accent")}
    >
      <span className="w-12 shrink-0 font-mono tabular-nums">{hm(item.start, plan.tz)}</span>
      <span>
        free · {duration(Math.round((item.end - item.start) / 60))}
        <span className="max-md:hidden"> — drop a task here</span>
      </span>
    </li>
  );
}

function Row({ plan, item, today }: { plan: Plan; item: Item; today: string }) {
  const editing = useEditing();
  const tz = plan.tz;
  if (item.kind === "free") return <FreeRow plan={plan} item={item} today={today} />;
  if (item.kind === "event")
    return (
      <li className="flex min-h-11 items-start gap-3 px-3 py-2">
        <Time start={hm(item.start, tz)} end={hm(item.end, tz)} />
        <span className={cn("flex min-w-0 flex-1 flex-col border-l-2 pl-3", item.event.busy ? "border-muted-foreground/50" : "border-dashed border-muted-foreground/30")}>
          <span className="text-sm break-words">{item.event.title}</span>
          <span className="text-xs text-muted-foreground">{item.event.busy ? "Calendar" : "Calendar · free time"}</span>
        </span>
      </li>
    );
  if (item.kind === "reminder")
    return (
      <li className="flex min-h-11 items-center gap-3 px-3 py-2">
        <Time start={hm(item.start, tz)} />
        <Bell className="size-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 text-sm break-words">{item.reminder.title}</span>
      </li>
    );
  const task = item.task;
  const done = task.fields.state !== "open";
  return (
    <li {...(done ? {} : dragProps(task))} className={cn("flex min-h-11 items-center gap-3 px-3 py-2", !done && "cursor-grab active:cursor-grabbing")}>
      <Time start={hm(item.start, tz)} end={hm(item.end, tz)} />
      <TaskCheck task={task} />
      <button type="button" onClick={() => editing?.open("tasks", task.id)} className="flex min-w-0 flex-1 flex-col items-start gap-0.5 text-left">
        <span className={cn("text-sm break-words", done && "text-muted-foreground line-through")}>{task.fields.title || "Untitled"}</span>
        <TaskMeta plan={plan} task={task} today={today} />
      </button>
      {!done && <PlanMenu plan={plan} task={task} today={today} label="Move" />}
    </li>
  );
}

function Candidates({ plan, today }: { plan: Plan; today: string }) {
  const editing = useEditing();
  const rows = plan.tasks.rows
    .filter((t) => t.fields.state === "open" && slotOf(t) === null)
    .sort((a, b) => (a.fields.due ?? "9999").localeCompare(b.fields.due ?? "9999") || a.created - b.created);
  if (!rows.length) return <p className="px-1 text-xs text-muted-foreground">Every open task has a day.</p>;
  return (
    <section className="flex flex-col gap-2">
      <h2 className="flex items-baseline gap-2 px-1 text-[13px] font-medium">
        To plan <span className="font-mono text-xs font-normal text-muted-foreground tabular-nums">{rows.length}</span>
      </h2>
      <ul className={LIST}>
        {rows.map((t) => (
          <li key={t.id} {...dragProps(t)} className="flex cursor-grab items-start gap-2 px-3 py-2 active:cursor-grabbing">
            <button type="button" onClick={() => editing?.open("tasks", t.id)} className="flex min-w-0 flex-1 flex-col items-start gap-0.5 text-left">
              <span className="text-[13px] break-words">{t.fields.title || "Untitled"}</span>
              <TaskMeta plan={plan} task={t} today={today} />
            </button>
            <PlanMenu plan={plan} task={t} today={today} />
          </li>
        ))}
      </ul>
    </section>
  );
}

export function DayTab({ plan, today, reload }: { plan: Plan; today: string; reload: () => void }) {
  const [day, setDay] = useState(today);
  const items = useMemo(() => dayItems(plan, day), [plan, day]);
  const allDay = plan.calendar.events.filter((e) => e.all_day && dayOf(e.start, plan.tz) <= day && dayOf(e.end - 1, plan.tz) >= day);
  const age = plan.calendar.at ? Math.round((plan.now - plan.calendar.at) / 60) : null;
  return (
    <div className="mx-auto grid w-full max-w-6xl gap-6 px-4 py-5 md:px-6 lg:grid-cols-[minmax(0,1fr)_18rem]">
      <div className="flex min-w-0 flex-col gap-4">
        <Strip plan={plan} today={today} day={day} onPick={setDay} />
        <Summary plan={plan} day={day} />
        <section className="flex flex-col gap-2">
          <h2 className="flex flex-wrap items-baseline gap-2 px-1 text-[13px] font-medium">
            {dayWords(day, today)}
            {day !== today && <span className="font-normal text-muted-foreground">{dayWords(day, "", false)}</span>}
            {allDay.map((e, n) => (
              <span key={n} className="rounded border px-1.5 text-xs font-normal text-muted-foreground">{e.title}</span>
            ))}
            {plan.calendar.stale && (
              <span className="ml-auto text-xs font-normal text-muted-foreground">
                {age === null ? "the calendar has not been read yet" : `calendar read ${duration(Math.max(age, 1))} ago`}
              </span>
            )}
          </h2>
          {items.length ? (
            <ul className={LIST}>
              {items.map((i) => (
                <Row key={`${i.kind}:${i.start}:${i.kind === "task" ? i.task.id : i.kind === "reminder" ? i.reminder.id : ""}`} plan={plan} item={i} today={today} />
              ))}
            </ul>
          ) : (
            <p className="px-1 text-sm text-muted-foreground">Nothing on this day, and its hours are over.</p>
          )}
        </section>
      </div>
      <aside className="flex min-w-0 flex-col gap-6">
        <NeedsYou plan={plan} today={today} reload={reload} compact />
        <Candidates plan={plan} today={today} />
      </aside>
    </div>
  );
}
