// Tasks: everything to do in one list — what asks for a task («From people»: a person's ask, from the feed, and a
// reminder), what has no day yet («To plan»), what has one («Planned») — by when, or by goal. A row opens its record
// in the drawer.
import { useMemo, useState } from "react";
import { useEditing } from "@/components/views/edit";
import { Ban, Bell, Clock, Mail, MessageCircle } from "@/components/icons";
import { Dialog } from "@/components/ui/dialog";
import { WaitingPane } from "@/components/waiting-sheet";
import { pressAsk, useAsks } from "@/lib/asks";
import { type FeedRow, useFeed, waited } from "@/lib/feed";
import { dayOf, dayWords, hm, named, nextBlock, type Plan, type Task, windowDay } from "@/lib/plan";
import { cn, LIST } from "@/lib/utils";
import { PlanMenu, Popover, RowPlan, TaskCheck, TaskMeta, useSchedule } from "./shared";

const REMINDER_DAYS = 7;

function Section({ title, count, hint, children }: { title: string; count?: number; hint?: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h2 className="flex items-baseline gap-2 px-1 text-[13px] font-medium">
        {title}
        {count !== undefined && <span className="font-mono text-xs font-normal text-muted-foreground tabular-nums">{count}</span>}
        {hint && <span className="ml-auto text-xs font-normal text-muted-foreground">{hint}</span>}
      </h2>
      <ul className={LIST}>{children}</ul>
    </section>
  );
}

export function TaskRow({ plan, task, today }: { plan: Plan; task: Task; today: string }) {
  const editing = useEditing();
  const next = nextBlock(plan, task);
  const slot = next ? next.start : null;
  const done = task.fields.state !== "open";
  return (
    <li className="flex min-h-11 items-center gap-3 px-3 py-2">
      <TaskCheck task={task} />
      <button
        type="button"
        onClick={() => editing?.open("tasks", task.id)}
        className="flex min-w-0 flex-1 flex-col items-start gap-0.5 text-left"
      >
        <span className={cn("max-w-full text-sm break-words", done && "text-muted-foreground line-through")}>
          {task.fields.title || "Untitled"}
        </span>
        <TaskMeta plan={plan} task={task} today={today} />
      </button>
      <span className="shrink-0 font-mono text-xs text-muted-foreground tabular-nums max-sm:hidden">
        {slot !== null ? `${dayWords(windowDay(slot, plan.plan.window), today, true)} ${hm(slot, plan.tz)}` : "no day"}
      </span>
      {!done && <PlanMenu plan={plan} task={task} today={today} label={slot !== null ? "Move" : "Plan"} />}
    </li>
  );
}

function AddTask() {
  const { add } = useSchedule();
  const [title, setTitle] = useState("");
  return (
    <form
      className="flex items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        const text = title.trim();
        if (!text) return;
        void add({ title: text }, "Added.").then((ok) => ok && setTitle(""));
      }}
    >
      <input
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="Add a task"
        aria-label="Add a task"
        maxLength={200}
        className="h-9 min-w-0 flex-1 rounded-md border bg-background px-3 text-sm placeholder:text-muted-foreground max-md:h-11"
      />
      <button type="submit" disabled={!title.trim()} className="h-9 rounded-md bg-primary px-3 text-sm font-medium text-primary-foreground disabled:opacity-50 max-md:h-11">
        Add
      </button>
    </form>
  );
}

/** A person's ask: «Done» closes it, «Plan» makes its task and lays it, «Tomorrow» and «Not a task» are under «⋯»;
 * its title opens what it is about — her words, the sender, the thread — in a sheet. */
function AskRow({ plan, row, bot, today }: { plan: Plan; row: FeedRow; bot: string; today: string }) {
  const [open, setOpen] = useState(false);
  const t = row.t as string;
  const title = row.essence;
  return (
    <li className="flex min-h-11 items-center gap-3 px-3 py-2">
      <button
        type="button"
        aria-label={`Done: ${title}`}
        onClick={() => void pressAsk(t, "done", "Done.")}
        className="hit inline-flex size-[18px] shrink-0 items-center justify-center rounded-full border border-muted-foreground/50 transition-colors hover:border-foreground"
      />
      <button type="button" onClick={() => setOpen(true)} className="flex min-w-0 flex-1 flex-col items-start gap-0.5 text-left">
        <span className="max-w-full text-sm break-words">{title}</span>
        <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
          {row.source === "chat" ? <MessageCircle className="size-3.5" /> : <Mail className="size-3.5" />}
          {row.title}
          <span aria-hidden>·</span>
          <span className="tabular-nums">waits {waited(row.since, plan.now)}</span>
        </span>
      </button>
      <RowPlan plan={plan} by={{ t }} what={title} today={today} />
      <Popover label={`More: ${title}`} button="⋯">
        {(close) => (
          <div className="flex flex-col text-[13px]">
            {([["later", "Tomorrow", Clock], ["dismissed", "Not a task", Ban]] as const).map(([action, label, Icon]) => (
              <button
                key={action}
                type="button"
                role="menuitem"
                onClick={() => {
                  close();
                  void pressAsk(t, action, `${label}.`);
                }}
                className="flex h-8 items-center gap-2 rounded-md px-2 text-left transition-colors hover:bg-accent max-md:h-10"
              >
                <Icon className="size-4 text-muted-foreground" />
                {label}
              </button>
            ))}
          </div>
        )}
      </Popover>
      <Dialog open={open} onClose={() => setOpen(false)} title="From people" sheet side>
        <div className="p-4">
          <WaitingPane row={row} bot={bot} now={plan.now} />
        </div>
      </Dialog>
    </li>
  );
}

/** What people asked and the reminders of the week that no open task names yet: each becomes a task by «Plan». */
export function FromPeople({ plan, today, compact = false }: { plan: Plan; today: string; compact?: boolean }) {
  const feed = useFeed();
  const asks = useAsks(feed, plan);
  const taken = named(plan);
  const horizon = plan.now + REMINDER_DAYS * 86400;
  const reminders = plan.reminders.filter((r) => !taken.has(r.id) && r.at <= horizon);
  if (!asks.length && !reminders.length) return null;
  return (
    <Section title="From people" count={asks.length + reminders.length} hint={compact ? undefined : "plan it, or close it"}>
      {asks.map((r) => <AskRow key={r.key} plan={plan} row={r} bot={feed?.bot ?? ""} today={today} />)}
      {reminders.map((r) => (
        <li key={r.id} className={cn("flex min-h-11 items-center gap-3 px-3 py-2", compact && "flex-wrap")}>
          <Bell className="size-4 shrink-0 text-muted-foreground" />
          <span className="flex min-w-0 flex-1 flex-col">
            <span className="text-sm break-words">{r.title}</span>
            <span className="font-mono text-xs text-muted-foreground tabular-nums">
              {dayWords(dayOf(r.at, plan.tz), today, true)} {hm(r.at, plan.tz)}
              {r.repeat ? ` · ${r.repeat}` : ""}
            </span>
          </span>
          <RowPlan plan={plan} by={{ ref: r.id }} what={r.title} today={today} />
        </li>
      ))}
    </Section>
  );
}

type Grouping = "when" | "goal";

export function TasksTab({ plan, today }: { plan: Plan; today: string }) {
  const [by, setBy] = useState<Grouping>("when");
  const open = useMemo(() => plan.tasks.rows.filter((t) => t.fields.state === "open"), [plan]);
  const unplanned = useMemo(
    () => open.filter((t) => !nextBlock(plan, t)).sort((a, b) => (a.fields.due ?? "9999").localeCompare(b.fields.due ?? "9999") || a.created - b.created),
    [plan, open],
  );
  const planned = useMemo(
    () => open.filter((t) => nextBlock(plan, t)).sort((a, b) => (nextBlock(plan, a)?.start ?? 0) - (nextBlock(plan, b)?.start ?? 0)),
    [plan, open],
  );
  const doneToday = plan.tasks.rows.filter((t) => t.fields.state === "done" && dayOf(t.updated, plan.tz) === today);
  const groups = useMemo(() => {
    const out = plan.tasks.goals.map((g) => ({ key: g.value, title: g.label, rows: open.filter((t) => t.fields.goal === g.value) }));
    out.push({ key: "", title: "No goal", rows: open.filter((t) => !plan.tasks.goals.some((g) => g.value === t.fields.goal)) });
    return out.filter((g) => g.rows.length);
  }, [plan, open]);
  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 px-4 py-5 md:px-6">
      <div className="flex flex-wrap items-center gap-3">
        <fieldset aria-label="Group the tasks" className="inline-flex h-9 rounded-md border p-0.5 max-md:h-11">
          {(["when", "goal"] as const).map((g) => (
            <button
              key={g}
              type="button"
              aria-pressed={by === g}
              onClick={() => setBy(g)}
              className={cn("rounded px-3 text-[13px] transition-colors", by === g ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground")}
            >
              {g === "when" ? "By when" : "By goal"}
            </button>
          ))}
        </fieldset>
        <span className="text-xs text-muted-foreground tabular-nums">
          {open.length} open · {unplanned.length} without a day{doneToday.length ? ` · ${doneToday.length} done today` : ""}
        </span>
      </div>
      <AddTask />
      <FromPeople plan={plan} today={today} />
      {by === "when" ? (
        <>
          {unplanned.length > 0 && (
            <Section title="To plan" count={unplanned.length} hint="pick a day for each">
              {unplanned.map((t) => <TaskRow key={t.id} plan={plan} task={t} today={today} />)}
            </Section>
          )}
          {planned.length > 0 && (
            <Section title="Planned" count={planned.length} hint="soonest first">
              {planned.map((t) => <TaskRow key={t.id} plan={plan} task={t} today={today} />)}
            </Section>
          )}
        </>
      ) : (
        groups.map((g) => (
          <Section key={g.key} title={g.title} count={g.rows.length}>
            {g.rows.map((t) => <TaskRow key={t.id} plan={plan} task={t} today={today} />)}
          </Section>
        ))
      )}
      {doneToday.length > 0 && (
        <Section title="Done today" count={doneToday.length}>
          {doneToday.map((t) => <TaskRow key={t.id} plan={plan} task={t} today={today} />)}
        </Section>
      )}
      {!open.length && !doneToday.length && (
        <p className="px-1 text-sm text-muted-foreground">No open tasks. Add one above, or tell the assistant in Telegram what to do.</p>
      )}
    </div>
  );
}
