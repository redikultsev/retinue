// Day: one window of the plan's (the owner's day in the plan's zone, shown in his own) as it will go — a strip of the
// week to pick it, «Plan my day» and the plan the Router laid (what was left out, «Undo», «Plan again»), «Busy now»,
// then the window in order: the calendar's meetings, the blocks of tasks, the reminders at their time, the free
// stretches, onto which a task or a block is dragged to stand there as the owner's own.
import { useMemo, useState } from "react";
import { useEditing } from "@/components/views/edit";
import { Bell, Close } from "@/components/icons";
import {
  addDays,
  type CalEvent,
  dayWords,
  duration,
  goalSwatch,
  hm,
  type Placed,
  type Plan,
  placed,
  planDay,
  type PlanReminder,
  weekday,
  windowDay,
  windowOf,
  ymd,
} from "@/lib/plan";
import { cn, LIST } from "@/lib/utils";
import { dragProps, PlanMenu, Popover, press, TaskCheck, TaskMeta, useDrop, useOwnBlock } from "./shared";
import { NeedsYou } from "./tasks-tab";

const STRIP_DAYS = 7;
const GAP_MIN = 15;
const WEEKDAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

type Item =
  | { kind: "event"; start: number; end: number; event: CalEvent }
  | { kind: "block"; start: number; end: number; placed: Placed }
  | { kind: "reminder"; start: number; end: number; reminder: PlanReminder }
  | { kind: "hold"; start: number; end: number; label: string }
  | { kind: "free"; start: number; end: number };

/** What a window holds, in its order, with its free stretches between — none in the past. */
export function dayItems(plan: Plan, day: string): Item[] {
  const [open, close] = windowOf(day, plan.plan.window);
  const inside = (s: number, e: number) => e > open && s < close;
  const items: Item[] = [];
  for (const e of plan.calendar.events) if (!e.all_day && inside(e.start, e.end)) items.push({ kind: "event", start: e.start, end: e.end, event: e });
  for (const p of placed(plan)) if (inside(p.start, p.end)) items.push({ kind: "block", start: p.start, end: p.end, placed: p });
  for (const r of plan.reminders) if (r.at >= open && r.at < close) items.push({ kind: "reminder", start: r.at, end: r.at, reminder: r });
  for (const h of plan.plan.holds) if (inside(h.from, h.to)) items.push({ kind: "hold", start: h.from, end: h.to, label: h.label });
  const busy = items
    .filter((i) => (i.kind === "event" && i.event.busy) || (i.kind === "block" && i.placed.block.fields.state === "planned") || i.kind === "hold")
    .map((i) => [i.start, i.end] as [number, number])
    .sort((a, b) => a[0] - b[0]);
  let cursor = Math.max(open, Math.ceil(plan.now / 900) * 900);
  for (const [a, b] of busy) {
    if (a - cursor >= GAP_MIN * 60 && cursor < close) items.push({ kind: "free", start: cursor, end: Math.min(a, close) });
    cursor = Math.max(cursor, b);
  }
  if (close - cursor >= GAP_MIN * 60) items.push({ kind: "free", start: cursor, end: close });
  return items.sort((x, y) => x.start - y.start || (x.kind === "free" ? 1 : 0) - (y.kind === "free" ? 1 : 0));
}

/** How much of a window is free from now, and how much of it is planned, in minutes. */
export function load(plan: Plan, day: string): { planned: number; free: number } {
  let planned = 0;
  let free = 0;
  for (const i of dayItems(plan, day)) {
    if (i.kind === "block" && i.placed.block.fields.state === "planned" && i.end > plan.now)
      planned += Math.round((i.end - Math.max(i.start, plan.now)) / 60);
    if (i.kind === "free") free += Math.round((i.end - i.start) / 60);
  }
  return { planned, free };
}

function Strip({ plan, first, day, onPick }: { plan: Plan; first: string; day: string; onPick: (d: string) => void }) {
  return (
    <div role="tablist" aria-label="Days" className="grid grid-cols-7 gap-1.5 max-md:flex max-md:overflow-x-auto max-md:pb-1">
      {Array.from({ length: STRIP_DAYS }, (_, n) => addDays(first, n)).map((d) => {
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
              {d === first ? "Today" : WEEKDAY[weekday(d)]}
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

/** The plan of the window: «Plan my day» before there is one; after it, what it left out, «Undo», «Plan again». */
function PlanCard({ plan, day, today }: { plan: Plan; day: string; today: string }) {
  const { planned, free } = load(plan, day);
  const entry = plan.plan.plans[day];
  const live = !!entry && !entry.undone && !entry.failed;
  const [open, close] = windowOf(day, plan.plan.window);
  const said = plan.plan.said?.text && (plan.plan.said.at ?? 0) > plan.now - 600 ? plan.plan.said.text : null;
  const byGoal = new Map<string, number>();
  for (const i of dayItems(plan, day))
    if (i.kind === "block" && i.placed.block.fields.state === "planned") {
      const g = i.placed.task?.fields.goal ?? "";
      byGoal.set(g, (byGoal.get(g) ?? 0) + Math.round((i.end - i.start) / 60));
    }
  const total = planned + free;
  return (
    <div className="flex flex-col gap-3 rounded-xl border bg-card px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <p className="flex flex-wrap items-baseline gap-x-2">
          <span className="text-xl font-medium tabular-nums">{duration(planned)}</span>
          <span className="text-sm text-muted-foreground">
            planned of {duration(total)} free, {hm(open, plan.tz)}–{hm(close, plan.tz)}
          </span>
        </p>
        <div className="ml-auto flex items-center gap-2">
          {live ? (
            <>
              <button
                type="button"
                onClick={() => void press({ action: "undo", plan: entry.id }, "Taking the plan back…")}
                className="h-8 rounded-md border px-3 text-[13px] transition-colors hover:bg-accent max-md:h-11"
              >
                Undo
              </button>
              <button
                type="button"
                onClick={() => void press({ action: "plan", day }, "Planning again…")}
                className="h-8 rounded-md border px-3 text-[13px] transition-colors hover:bg-accent max-md:h-11"
              >
                Plan again
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => void press({ action: "plan", day }, `Planning ${dayWords(day, today).toLowerCase()}…`)}
              className="h-8 rounded-md bg-primary px-3 text-[13px] font-medium text-primary-foreground transition-colors hover:bg-primary/90 max-md:h-11"
            >
              Plan my day
            </button>
          )}
          {day === today && <BusyMenu plan={plan} />}
        </div>
      </div>
      {total > 0 && byGoal.size > 0 && (
        <div className="flex h-2 overflow-hidden rounded-full bg-muted" aria-hidden>
          {[...byGoal].map(([g, m]) => (
            <span key={g} className={cn("h-full border-r-2 border-card last:border-r-0", goalSwatch(plan.tasks.goals, g))} style={{ width: `${(m / total) * 100}%` }} />
          ))}
        </div>
      )}
      {live && entry.left.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Left out: {entry.left.map((l) => l.title).filter(Boolean).join(", ")} — no free stretch long enough.
        </p>
      )}
      {said && <p className="text-xs text-muted-foreground">{said}</p>}
    </div>
  );
}

/** «Busy now»: the time from now held, and the plan's blocks it crosses laid again after it. */
function BusyMenu({ plan }: { plan: Plan }) {
  const held = plan.plan.holds.find((h) => h.from <= plan.now && h.to > plan.now);
  if (held)
    return (
      <button
        type="button"
        onClick={() => void press({ action: "free" }, "Freeing the time…")}
        className="inline-flex h-8 items-center gap-1.5 rounded-md border px-3 text-[13px] transition-colors hover:bg-accent max-md:h-11"
      >
        Busy till {hm(held.to, plan.tz)}
        <Close className="size-3.5" />
      </button>
    );
  return (
    <Popover label="Busy now" button="Busy now">
      {(close) => (
        <div className="flex flex-col text-[13px]">
          {([30, 60, 120] as const).map((m) => (
            <button
              key={m}
              type="button"
              role="menuitem"
              onClick={() => {
                close();
                void press({ action: "busy", minutes: m }, `Busy for ${duration(m)}: moving what it crosses…`);
              }}
              className="flex h-8 items-center rounded-md px-2 text-left transition-colors hover:bg-accent max-md:h-10"
            >
              {duration(m)}
            </button>
          ))}
        </div>
      )}
    </Popover>
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

function FreeRow({ plan, item, today, all }: { plan: Plan; item: Item; today: string; all: Placed[] }) {
  const own = useOwnBlock();
  const drop = useDrop(
    plan,
    ({ placed: p, task }) => {
      const what = `Planned: ${dayWords(windowDay(item.start, plan.plan.window), today)}, ${hm(item.start, plan.tz)}.`;
      void (p ? own.move(p, item.start, what) : own.put(task, item.start, task.fields.minutes || 30, what));
    },
    all,
  );
  return (
    <li {...drop.props} className={cn("flex min-h-10 items-center gap-3 px-3 py-1.5 text-xs text-muted-foreground transition-colors", drop.over && "bg-accent")}>
      <span className="w-12 shrink-0 font-mono tabular-nums">{hm(item.start, plan.tz)}</span>
      <span>
        free · {duration(Math.round((item.end - item.start) / 60))}
        <span className="max-md:hidden"> — drop a task here</span>
      </span>
    </li>
  );
}

function Row({ plan, item, today, all }: { plan: Plan; item: Item; today: string; all: Placed[] }) {
  const editing = useEditing();
  const tz = plan.tz;
  if (item.kind === "free") return <FreeRow plan={plan} item={item} today={today} all={all} />;
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
  if (item.kind === "hold")
    return (
      <li className="flex min-h-10 items-center gap-3 bg-muted/40 px-3 py-1.5">
        <Time start={hm(item.start, tz)} end={hm(item.end, tz)} />
        <span className="min-w-0 flex-1 text-sm text-muted-foreground">{item.label}</span>
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
  const { block, task } = item.placed;
  const passed = block.fields.state === "passed";
  const done = task?.fields.state !== "open";
  return (
    <li {...(passed ? {} : dragProps({ block: block.id }))} className={cn("flex min-h-11 items-center gap-3 px-3 py-2", !passed && "cursor-grab active:cursor-grabbing")}>
      <Time start={hm(item.start, tz)} end={hm(item.end, tz)} />
      {task ? <TaskCheck task={task} /> : <span className="size-[18px]" />}
      <button type="button" onClick={() => task && editing?.open("tasks", task.id)} className="flex min-w-0 flex-1 flex-col items-start gap-0.5 text-left">
        <span className={cn("text-sm break-words", (done || passed) && "text-muted-foreground", done && "line-through")}>{task?.fields.title || "A task"}</span>
        <span className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
          {task && <TaskMeta plan={plan} task={task} today={today} />}
          {block.fields.why && <span className="text-warning">{block.fields.why}</span>}
          {passed && <span>counted</span>}
        </span>
      </button>
      {!passed && !done && (
        <button
          type="button"
          aria-label={`Take off the day: ${task?.fields.title ?? ""}`}
          onClick={() => void press({ action: "remove", block: block.id }, "Taking it off the day…")}
          className="inline-flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground max-md:size-11"
        >
          <Close className="size-4" />
        </button>
      )}
    </li>
  );
}

function Candidates({ plan, today, day }: { plan: Plan; today: string; day: string }) {
  const editing = useEditing();
  const inDay = new Set(dayItems(plan, day).flatMap((i) => (i.kind === "block" && i.placed.task ? [i.placed.task.id] : [])));
  const rows = plan.tasks.rows
    .filter((t) => t.fields.state === "open" && !inDay.has(t.id))
    .sort((a, b) => (a.fields.due ?? "9999").localeCompare(b.fields.due ?? "9999") || a.created - b.created);
  if (!rows.length) return <p className="px-1 text-xs text-muted-foreground">Every open task has a piece in this day.</p>;
  return (
    <section className="flex flex-col gap-2">
      <h2 className="flex items-baseline gap-2 px-1 text-[13px] font-medium">
        Not in this day <span className="font-mono text-xs font-normal text-muted-foreground tabular-nums">{rows.length}</span>
      </h2>
      <ul className={LIST}>
        {rows.map((t) => (
          <li key={t.id} {...dragProps({ task: t.id })} className="flex cursor-grab items-start gap-2 px-3 py-2 active:cursor-grabbing">
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

export function DayTab({ plan, reload }: { plan: Plan; today: string; reload: () => void }) {
  const first = planDay(plan.now, plan.plan.window);
  const [picked, setDay] = useState<string | null>(null);
  const day = picked ?? first;
  const items = useMemo(() => dayItems(plan, day), [plan, day]);
  const all = useMemo(() => placed(plan), [plan]);
  const [open] = windowOf(day, plan.plan.window);
  const allDay = plan.calendar.events.filter((e) => e.all_day && e.start <= open + 43200 && e.end > open);
  const age = plan.calendar.at ? Math.round((plan.now - plan.calendar.at) / 60) : null;
  return (
    <div className="mx-auto grid w-full max-w-6xl gap-6 px-4 py-5 md:px-6 lg:grid-cols-[minmax(0,1fr)_18rem]">
      <div className="flex min-w-0 flex-col gap-4">
        <Strip plan={plan} first={first} day={day} onPick={setDay} />
        <PlanCard plan={plan} day={day} today={first} />
        <section className="flex flex-col gap-2">
          <h2 className="flex flex-wrap items-baseline gap-2 px-1 text-[13px] font-medium">
            {dayWords(day, first)}
            {day !== first && <span className="font-normal text-muted-foreground">{dayWords(day, "", false)}</span>}
            {allDay.map((e, n) => (
              <span key={n} className="rounded border px-1.5 text-xs font-normal text-muted-foreground">
                {e.title}
              </span>
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
                <Row
                  key={`${i.kind}:${i.start}:${i.kind === "block" ? i.placed.block.id : i.kind === "reminder" ? i.reminder.id : ""}`}
                  plan={plan}
                  item={i}
                  today={first}
                  all={all}
                />
              ))}
            </ul>
          ) : (
            <p className="px-1 text-sm text-muted-foreground">Nothing in this day, and its time is over.</p>
          )}
        </section>
      </div>
      <aside className="flex min-w-0 flex-col gap-6">
        <NeedsYou plan={plan} today={first} reload={reload} compact />
        <Candidates plan={plan} today={first} day={day} />
      </aside>
    </div>
  );
}
