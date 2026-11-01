// Week: seven days side by side from today — each with how full its hours are, its meetings, tasks and reminders in
// order — and above them the week's planned time by goal. A task dragged to another day keeps its time of day there
// and stays where the owner put it.
import { useMemo } from "react";
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
  iso,
  minutesOf,
  type Plan,
  slotOf,
  ymd,
  type Task,
} from "@/lib/plan";
import { cn } from "@/lib/utils";
import { dayItems, load } from "./day-tab";
import { dragProps, TaskCheck, useDrop, useSetTask } from "./shared";

const WEEK_DAYS = 7;

function Column({ plan, day, today }: { plan: Plan; day: string; today: string }) {
  const set = useSetTask();
  const editing = useEditing();
  const tz = plan.tz;
  const items = dayItems(plan, day).filter((i) => i.kind !== "free");
  const allDay = plan.calendar.events.filter((e) => e.all_day && dayOf(e.start, tz) <= day && dayOf(e.end - 1, tz) >= day);
  const { planned, free } = load(plan, day);
  const total = planned + free;
  const drop = useDrop(plan, (task: Task) => {
    const was = slotOf(task);
    const [h = 18, m = 0] = (was !== null ? hm(was, tz) : "18:00").split(":").map(Number);
    const start = at(day, h * 60 + m, tz);
    void set(task, { slot: iso(start), locked: true }, `Moved to ${dayWords(day, today)}, ${hm(start, tz)}.`);
  });
  const [, , dd] = ymd(day);
  return (
    <section
      {...drop.props}
      aria-label={dayWords(day, today)}
      className={cn("flex w-[10rem] shrink-0 grow basis-[10rem] flex-col gap-2 rounded-lg p-1.5 transition-colors", drop.over && "bg-accent/60")}
    >
      <header className="flex flex-col gap-1.5 px-1">
        <h3 className="flex items-baseline gap-1.5 text-[13px]">
          <span className="font-mono text-base font-medium tabular-nums">{dd}</span>
          <span className={cn(day === today ? "text-foreground" : "text-muted-foreground")}>{dayWords(day, today, true).replace(/ \d+$/, "")}</span>
        </h3>
        <div className="flex h-1.5 overflow-hidden rounded-full bg-muted" aria-hidden>
          {total > 0 && <span className="h-full bg-warning/70" style={{ width: `${(planned / total) * 100}%` }} />}
        </div>
        <span className="font-mono text-[11px] text-muted-foreground tabular-nums">
          {duration(planned)} / {duration(total)}
        </span>
      </header>
      {allDay.map((e, n) => (
        <div key={n} className="rounded-md border border-dashed px-2 py-1 text-xs text-muted-foreground break-words">{e.title}</div>
      ))}
      <ol className="flex flex-col gap-1.5">
        {items.map((i) => {
          if (i.kind === "event")
            return (
              <li key={`e${i.start}${i.event.title}`} className="flex flex-col px-2 py-0.5 text-xs">
                <span className="font-mono text-muted-foreground tabular-nums">{hm(i.start, tz)}</span>
                <span className={cn("break-words", i.event.busy ? "text-foreground/80" : "text-muted-foreground")}>{i.event.title}</span>
              </li>
            );
          if (i.kind === "reminder")
            return (
              <li key={i.reminder.id} className="flex items-start gap-1.5 px-2 py-0.5 text-xs">
                <Bell className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                <span className="min-w-0 break-words">
                  <span className="font-mono text-muted-foreground tabular-nums">{hm(i.start, tz)}</span> {i.reminder.title}
                </span>
              </li>
            );
          if (i.kind !== "task") return null;
          const task = i.task;
          const done = task.fields.state !== "open";
          return (
            <li
              key={task.id}
              {...(done ? {} : dragProps(task))}
              className={cn("flex flex-col gap-1 rounded-md border bg-card px-2 py-1.5", !done && "cursor-grab active:cursor-grabbing")}
            >
              <span
                title={task.fields.locked ? "You put it here: it stays" : undefined}
                className="font-mono text-[11px] text-muted-foreground tabular-nums"
              >
                {hm(i.start, tz)}–{hm(i.end, tz)}
                {task.fields.locked && <span className="sr-only"> — you put it here</span>}
              </span>
              <span className="flex items-start gap-2">
                <TaskCheck task={task} className="mt-0.5" />
                <button
                  type="button"
                  onClick={() => editing?.open("tasks", task.id)}
                  className={cn("min-w-0 flex-1 text-left text-[13px] break-words", done && "text-muted-foreground line-through")}
                >
                  {task.fields.title || "Untitled"}
                </button>
              </span>
              {goalLabel(plan.tasks.goals, task.fields.goal) && (
                <span className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
                  <span aria-hidden className={cn("size-2 rounded-[2px]", goalSwatch(plan.tasks.goals, task.fields.goal))} />
                  {goalLabel(plan.tasks.goals, task.fields.goal)}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

export function WeekTab({ plan, today }: { plan: Plan; today: string }) {
  const days = Array.from({ length: WEEK_DAYS }, (_, n) => addDays(today, n));
  const last = addDays(today, WEEK_DAYS - 1);
  const byGoal = useMemo(() => {
    const out = new Map<string, number>();
    for (const t of plan.tasks.rows) {
      const s = slotOf(t);
      if (s === null || t.fields.state === "dropped") continue;
      const d = dayOf(s, plan.tz);
      if (d < today || d > last) continue;
      out.set(t.fields.goal ?? "", (out.get(t.fields.goal ?? "") ?? 0) + minutesOf(t));
    }
    return out;
  }, [plan, today, last]);
  return (
    <div className="flex min-h-0 w-full flex-col gap-4 px-4 py-5 md:px-6">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span className="text-[13px] text-foreground">This week, by goal</span>
        {byGoal.size === 0 && <span>nothing planned yet</span>}
        {[...byGoal].map(([g, m]) => (
          <span key={g} className="inline-flex items-center gap-1.5">
            <span aria-hidden className={cn("size-2 rounded-[2px]", goalSwatch(plan.tasks.goals, g))} />
            {goalLabel(plan.tasks.goals, g) ?? "No goal"}
            <span className="font-mono text-foreground tabular-nums">{duration(m)}</span>
          </span>
        ))}
      </div>
      <div className="-mx-4 flex gap-2 overflow-x-auto px-4 pb-2 md:-mx-6 md:px-6">
        {days.map((d) => (
          <Column key={d} plan={plan} day={d} today={today} />
        ))}
      </div>
    </div>
  );
}
