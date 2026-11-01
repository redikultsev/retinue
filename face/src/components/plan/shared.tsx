// What the three tabs share: a task's check, its line of meta, the menu that plans it, and the writes behind them —
// each the owner's write of `tasks` through the views' editor, with its «Undo» and its question when the record moved.
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type * as React from "react";
import { useEditing } from "@/components/views/edit";
import { Check } from "@/components/icons";
import { toast } from "@/lib/toast";
import {
  addDays,
  at,
  dayOf,
  dayWords,
  DEFAULT_MINUTES,
  duration,
  firstFree,
  goalLabel,
  goalSwatch,
  hm,
  iso,
  minutesOf,
  type Plan,
  planApi,
  slotOf,
  type Task,
  type TaskFields,
  taken,
} from "@/lib/plan";
import { cn } from "@/lib/utils";

export const DURATIONS = [15, 30, 60, 90];
export const PLAN_DAYS = 7;

/** The owner's write of one task's fields: true once written. */
export function useSetTask() {
  const editing = useEditing();
  return (task: Task, fields: { [K in keyof TaskFields]?: TaskFields[K] | null }, what: string) =>
    editing
      ? editing.write("tasks", [{ op: { op: "set", id: task.id, version: task.version, fields }, target: task.id }], what)
      : Promise.resolve(false);
}

export function useAddTask() {
  const editing = useEditing();
  return (fields: TaskFields, what: string) =>
    editing ? editing.write("tasks", [{ op: { op: "add", fields: { state: "open", ...fields } }, target: null }], what) : Promise.resolve(false);
}

/** The circle before a task: a press closes it, or opens it again. */
export function TaskCheck({ task, className }: { task: Task; className?: string }) {
  const set = useSetTask();
  const done = task.fields.state === "done";
  return (
    <button
      type="button"
      aria-label={done ? `Open again: ${task.fields.title ?? ""}` : `Done: ${task.fields.title ?? ""}`}
      aria-pressed={done}
      onClick={() => void set(task, { state: done ? "open" : "done" }, done ? "Opened again." : "Done.")}
      className={cn(
        "hit inline-flex size-[18px] shrink-0 items-center justify-center rounded-full border border-muted-foreground/50 transition-colors hover:border-foreground",
        done && "border-transparent bg-muted-foreground/60 text-background",
        className,
      )}
    >
      {done && <Check className="size-3" />}
    </button>
  );
}

/** A task's goal as a swatch and its words. */
export function Goal({ plan, goal }: { plan: Plan; goal?: string }) {
  const label = goalLabel(plan.tasks.goals, goal);
  if (!label) return null;
  return (
    <span className="inline-flex items-center gap-1.5">
      <span aria-hidden className={cn("size-2 rounded-[2px]", goalSwatch(plan.tasks.goals, goal))} />
      {label}
    </span>
  );
}

/** The line under a task's title: its goal, how long, by when. */
export function TaskMeta({ plan, task, today }: { plan: Plan; task: Task; today: string }) {
  const due = task.fields.due;
  const late = due && due < today && task.fields.state === "open";
  const bits: React.ReactNode[] = [];
  if (goalLabel(plan.tasks.goals, task.fields.goal)) bits.push(<Goal key="g" plan={plan} goal={task.fields.goal} />);
  bits.push(<span key="m">{duration(minutesOf(task))}</span>);
  if (due) bits.push(<span key="d" className={cn(late && "text-destructive")}>by {dayWords(due, today, true)}</span>);
  if (task.fields.ask) bits.push(<span key="a">from {task.fields.ask.startsWith("reminder:") ? "a reminder" : "a message"}</span>);
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
      {bits.map((b, n) => (
        <span key={n} className="inline-flex items-center gap-2">
          {n > 0 && <span aria-hidden>·</span>}
          {b}
        </span>
      ))}
    </span>
  );
}

/** A small menu under its button — above it when the screen has no room below —, drawn over the page so a list's
 * rounded frame never clips it, and following its button when the page scrolls; closed by a press beside it or Esc. */
export function Popover({ label, children, button }: {
  label: string;
  children: (close: () => void) => React.ReactNode;
  button: React.ReactNode;
}) {
  const [place, setPlace] = useState<{ top?: number; bottom?: number; right: number } | null>(null);
  const anchor = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const close = () => setPlace(null);
  const measure = useCallback(() => {
    if (!anchor.current) return null;
    const r = anchor.current.getBoundingClientRect();
    const right = Math.max(8, window.innerWidth - r.right);
    return window.innerHeight - r.bottom > MENU_ROOM || r.top < MENU_ROOM
      ? { top: r.bottom + 4, right }
      : { bottom: window.innerHeight - r.top + 4, right };
  }, []);
  useEffect(() => {
    if (!place) return;
    const away = (e: PointerEvent) =>
      !menu.current?.contains(e.target as Node) && !anchor.current?.contains(e.target as Node) && setPlace(null);
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setPlace(null);
    const follow = (e: Event) => !menu.current?.contains(e.target as Node) && setPlace(measure());
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", esc);
    window.addEventListener("scroll", follow, true);
    window.addEventListener("resize", follow);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", esc);
      window.removeEventListener("scroll", follow, true);
      window.removeEventListener("resize", follow);
    };
  }, [place, measure]);
  return (
    <>
      <button
        ref={anchor}
        type="button"
        aria-haspopup="menu"
        aria-expanded={!!place}
        aria-label={label}
        onClick={() => setPlace(place ? null : measure())}
        className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md border bg-background px-2.5 text-[13px] text-foreground transition-colors hover:bg-accent max-md:h-11"
      >
        {button}
      </button>
      {place &&
        createPortal(
          <div
            ref={menu}
            role="menu"
            style={place}
            className="fixed z-50 max-h-[80dvh] w-64 overflow-y-auto rounded-lg border bg-card p-2 text-card-foreground shadow-lg"
          >
            {children(close)}
          </div>,
          document.body,
        )}
    </>
  );
}

const MENU_ROOM = 420;

/** «Plan»: a day of the next week and how long — the first free time of it, in its evening — or an exact time,
 * which is the owner's and stays where he put it (`locked`). A planned task can be taken off the day. */
export function PlanMenu({ plan, task, today, label = "Plan" }: { plan: Plan; task: Task; today: string; label?: string }) {
  const set = useSetTask();
  const [minutes, setMinutes] = useState(task.fields.minutes ?? DEFAULT_MINUTES);
  const slot = slotOf(task);
  const [exact, setExact] = useState(() => (slot ? localInput(slot, plan.tz) : ""));
  const days = Array.from({ length: PLAN_DAYS }, (_, n) => addDays(today, n));
  const place = (day: string, close: () => void) => {
    const start = firstFree(day, minutes, taken(plan, task.id), plan.now, plan.tz);
    close();
    void set(task, { slot: iso(start), minutes, locked: false }, `Planned: ${dayWords(day, today)}, ${hm(start, plan.tz)}.`);
  };
  return (
    <Popover label={`${label}: ${task.fields.title ?? ""}`} button={label}>
      {(close) => (
        <div className="flex flex-col gap-2 text-[13px]">
          <fieldset className="flex gap-1" aria-label="How long">
            {DURATIONS.map((m) => (
              <button
                key={m}
                type="button"
                aria-pressed={minutes === m}
                onClick={() => setMinutes(m)}
                className={cn(
                  "h-8 flex-1 rounded-md border font-mono text-xs tabular-nums transition-colors max-md:h-10",
                  minutes === m ? "border-foreground bg-accent text-foreground" : "text-muted-foreground hover:bg-accent",
                )}
              >
                {duration(m)}
              </button>
            ))}
          </fieldset>
          <div className="flex flex-col">
            {days.map((day) => (
              <button
                key={day}
                type="button"
                role="menuitem"
                onClick={() => place(day, close)}
                className="flex h-8 items-center justify-between rounded-md px-2 text-left transition-colors hover:bg-accent max-md:h-10"
              >
                <span>{dayWords(day, today)}</span>
                <span className="font-mono text-xs text-muted-foreground tabular-nums">
                  {hm(firstFree(day, minutes, taken(plan, task.id), plan.now, plan.tz), plan.tz)}
                </span>
              </button>
            ))}
          </div>
          <form
            className="flex items-center gap-1.5 border-t pt-2"
            onSubmit={(e) => {
              e.preventDefault();
              const start = fromLocalInput(exact, plan.tz);
              if (start === null) return toast("Pick a day and a time.", "error");
              close();
              void set(task, { slot: iso(start), minutes, locked: true },
                `Planned: ${dayWords(dayOf(start, plan.tz), today)}, ${hm(start, plan.tz)}.`);
            }}
          >
            <input
              type="datetime-local"
              aria-label="At an exact time"
              value={exact}
              step={900}
              onChange={(e) => setExact(e.target.value)}
              className="h-8 min-w-0 flex-1 rounded-md border bg-background px-2 font-mono text-xs max-md:h-10"
            />
            <button type="submit" className="h-8 rounded-md border px-2.5 transition-colors hover:bg-accent max-md:h-10">
              Set
            </button>
          </form>
          {slot !== null && (
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                close();
                void set(task, { slot: null, locked: null }, "Taken off the day.");
              }}
              className="flex h-8 items-center rounded-md px-2 text-left text-muted-foreground transition-colors hover:bg-accent hover:text-foreground max-md:h-10"
            >
              Take off the day
            </button>
          )}
        </div>
      )}
    </Popover>
  );
}

/** A moment as `<input type=datetime-local>` takes it, in his zone. */
export function localInput(ts: number, tz: string): string {
  return `${dayOf(ts, tz)}T${hm(ts, tz)}`;
}

export function fromLocalInput(value: string, tz: string): number | null {
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})$/.exec(value);
  return m?.[1] ? at(m[1], Number(m[2]) * 60 + Number(m[3]), tz) : null;
}

/** «Make it a task» on an ask or a reminder: the Router makes it within a few seconds, and the list has it. */
export function MakeTask({ by, what, onMade }: { by: { t: string } | { ref: string }; what: string; onMade: () => void }) {
  const [busy, setBusy] = useState(false);
  return (
    <button
      type="button"
      disabled={busy}
      aria-label={`Make it a task: ${what}`}
      onClick={() => {
        setBusy(true);
        planApi.task(by).then(
          () => {
            toast("Making it a task…");
            setTimeout(onMade, 4000);
          },
          (e: unknown) => {
            setBusy(false);
            toast(e instanceof Error ? e.message : "Not made.", "error", 6000);
          },
        );
      }}
      className="inline-flex h-8 shrink-0 items-center rounded-md border bg-background px-2.5 text-[13px] transition-colors hover:bg-accent disabled:opacity-50 max-md:h-11"
    >
      {busy ? "Making…" : "Make it a task"}
    </button>
  );
}

/** Where a dragged task goes: its id travels in the drag, a drop gives it a slot that is the owner's own. */
export const DRAG_TYPE = "application/x-retinue-task";

export function dragProps(task: Task) {
  return {
    draggable: true,
    onDragStart: (e: React.DragEvent) => {
      e.dataTransfer.setData(DRAG_TYPE, task.id);
      e.dataTransfer.effectAllowed = "move";
    },
  };
}

export function useDrop(plan: Plan, onDrop: (task: Task) => void) {
  const [over, setOver] = useState(false);
  return {
    over,
    props: {
      onDragOver: (e: React.DragEvent) => {
        if (!e.dataTransfer.types.includes(DRAG_TYPE)) return;
        e.preventDefault();
        setOver(true);
      },
      onDragLeave: () => setOver(false),
      onDrop: (e: React.DragEvent) => {
        setOver(false);
        const id = e.dataTransfer.getData(DRAG_TYPE);
        const task = plan.tasks.rows.find((t) => t.id === id);
        if (task) {
          e.preventDefault();
          onDrop(task);
        }
      },
    },
  };
}
