// Tasks: everything to do in one list — what asks for a task («From people»: a person's ask, from the feed, and a
// reminder), then the open tasks by project, in the Base's tree («Projects»), or by whether they have a day («Days»:
// «To plan», «Planned»). A row opens its record in the drawer; its «⋯» sets its project.
import { useEffect, useMemo, useRef, useState } from "react";
import { useEditing } from "@/components/views/edit";
import { Ban, Bell, Check, Clock, Mail, MessageCircle, Plus } from "@/components/icons";
import { Dialog } from "@/components/ui/dialog";
import { WaitingPane } from "@/components/waiting-sheet";
import { pressAsk, useAsks } from "@/lib/asks";
import { type FeedRow, useFeed, waited } from "@/lib/feed";
import { live } from "@/lib/live";
import { byProject, dayOf, dayWords, hm, named, nextBlock, type Plan, type Task, type TaskGroup, windowDay } from "@/lib/plan";
import { projectLabel, projectName, projectTree, type TreeNode, useProjectOf, useProjects } from "@/lib/projects";
import type { Sphere } from "@/lib/types";
import { cn, LIST } from "@/lib/utils";
import { PlanMenu, Popover, RowPlan, TaskCheck, TaskMeta, useSchedule } from "./shared";

const REMINDER_DAYS = 7;
/** How many rows a project's group shows before «Show all». */
export const FOLDED = 5;
const NO_SPHERES: Sphere[] = [];

function Section({ title, count, hint, action, group, children }: {
  title: string;
  count?: number;
  hint?: string;
  action?: React.ReactNode;
  /** A project's group: its key, for the page to find. */
  group?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-2" data-group={group}>
      <h2 className="flex min-h-7 items-center gap-2 px-1 text-[13px] font-medium">
        <span className="min-w-0 break-words">{title}</span>
        {count !== undefined && <span className="font-mono text-xs font-normal text-muted-foreground tabular-nums">{count}</span>}
        {hint && <span className="ml-auto text-xs font-normal text-muted-foreground">{hint}</span>}
        {action && <span className={cn("flex", !hint && "ml-auto")}>{action}</span>}
      </h2>
      <ul className={LIST}>{children}</ul>
    </section>
  );
}

/** The projects a task can be in: the tree, in its order, the project a task is in now ("" none), a project's words
 * on a row («Work / Site») and its own name in the tree («Site»). */
export interface Projects {
  tree: TreeNode[];
  of: (task: Task) => string;
  label: (key: string) => string;
  name: (key: string) => string;
}

/** A row's «⋯»: the task's project — «No project», then the tree, a sub-project under its project. A pick is the
 * owner's write of the task's `project`, with its «Undo». */
export function ProjectMenu({ task, projects }: { task: Task; projects: Projects }) {
  const { set } = useSchedule();
  const current = projects.of(task);
  const title = task.fields.title ?? "";
  return (
    <Popover label={`Project: ${title}`} button="⋯">
      {(close) => (
        <div className="flex flex-col text-[13px]">
          <p className="px-2 pb-1 text-xs text-muted-foreground">Project</p>
          {[{ key: "", depth: 0 }, ...projects.tree].map(({ key, depth }) => (
            <button
              key={key || "none"}
              type="button"
              role="menuitemradio"
              aria-checked={key === current}
              data-project={key}
              onClick={() => {
                close();
                if (key === current) return;
                void set(task, { project: key || null }, key ? `Moved to ${projects.label(key)}.` : "Moved to No project.");
              }}
              style={{ paddingLeft: 8 + depth * 12 }}
              className="flex h-8 shrink-0 items-center gap-2 rounded-md pr-2 text-left transition-colors hover:bg-accent max-md:h-10"
            >
              <span className={cn("min-w-0 flex-1 truncate", !key && "text-muted-foreground")}>{key ? projects.name(key) : "No project"}</span>
              {key === current && <Check className="size-4 shrink-0 text-muted-foreground" />}
            </button>
          ))}
        </div>
      )}
    </Popover>
  );
}

export function TaskRow({ plan, task, today, projects, project }: {
  plan: Plan;
  task: Task;
  today: string;
  /** Where the row can be moved to another project: its «⋯». */
  projects?: Projects;
  /** Its project's words on its meta line, where the list is not by project. */
  project?: string;
}) {
  const editing = useEditing();
  const next = nextBlock(plan, task);
  const slot = next ? next.start : null;
  const done = task.fields.state !== "open";
  return (
    <li data-task={task.id} className="flex min-h-11 items-center gap-3 px-3 py-2">
      <TaskCheck task={task} />
      <button
        type="button"
        onClick={() => editing?.open("tasks", task.id)}
        className="flex min-w-0 flex-1 flex-col items-start gap-0.5 text-left"
      >
        <span className={cn("max-w-full text-sm break-words", done && "text-muted-foreground line-through")}>
          {task.fields.title || "Untitled"}
        </span>
        <TaskMeta plan={plan} task={task} today={today} project={project} />
      </button>
      <span className="shrink-0 font-mono text-xs text-muted-foreground tabular-nums max-sm:hidden">
        {slot !== null ? `${dayWords(windowDay(slot, plan.plan.window), today, true)} ${hm(slot, plan.tz)}` : "no day"}
      </span>
      {!done && <PlanMenu plan={plan} task={task} today={today} label={slot !== null ? "Move" : "Plan"} />}
      {!done && projects && <ProjectMenu task={task} projects={projects} />}
    </li>
  );
}

/** The box that adds a task by its title: into a project when it is that project's group's. */
export function AddTask({ project, onClose }: { project?: string; onClose?: () => void }) {
  const { add } = useSchedule();
  const [title, setTitle] = useState("");
  const input = useRef<HTMLInputElement>(null);
  // a group's box opens for a title at once: focused when it opens, never again while it is open
  const opened = !!onClose;
  useEffect(() => {
    if (opened) input.current?.focus();
  }, [opened]);
  return (
    <form
      className="flex items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        const text = title.trim();
        if (!text) return;
        void add(project ? { title: text, project } : { title: text }, "Added.").then((ok) => ok && setTitle(""));
      }}
    >
      <input
        ref={input}
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        onKeyDown={(e) => e.key === "Escape" && onClose?.()}
        placeholder="Add a task"
        aria-label="Add a task"
        maxLength={200}
        className="h-9 min-w-0 flex-1 rounded-md border bg-background px-3 text-sm placeholder:text-muted-foreground max-md:h-11"
      />
      <button type="submit" disabled={!title.trim()} className="h-9 rounded-md bg-primary px-3 text-sm font-medium text-primary-foreground disabled:opacity-50 max-md:h-11">
        Add
      </button>
      {onClose && (
        <button type="button" onClick={onClose} className="h-9 rounded-md px-2.5 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground max-md:h-11">
          Cancel
        </button>
      )}
    </form>
  );
}

/** A project's group: its first {@link FOLDED} rows, «Show all N» for the rest; «＋» adds a task into it. */
function ProjectGroup({ plan, group, today, projects }: { plan: Plan; group: TaskGroup; today: string; projects: Projects }) {
  const [all, setAll] = useState(false);
  const [adding, setAdding] = useState(false);
  const title = group.key ? projects.label(group.key) : "No project";
  const shown = all ? group.rows : group.rows.slice(0, FOLDED);
  return (
    <Section
      title={title}
      count={group.rows.length}
      group={group.key}
      action={
        <button
          type="button"
          aria-label={`Add a task to ${title}`}
          onClick={() => setAdding(true)}
          className="inline-flex size-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground max-md:size-11"
        >
          <Plus className="size-4" />
        </button>
      }
    >
      {adding && (
        <li className="px-3 py-2">
          <AddTask project={group.key || undefined} onClose={() => setAdding(false)} />
        </li>
      )}
      {shown.map((t) => <TaskRow key={t.id} plan={plan} task={t} today={today} projects={projects} />)}
      {group.rows.length > FOLDED && (
        <li>
          <button
            type="button"
            aria-expanded={all}
            data-fold
            onClick={() => setAll(!all)}
            className="flex min-h-10 w-full items-center px-3 text-left text-[13px] text-muted-foreground transition-colors hover:bg-accent/50 hover:text-foreground max-md:min-h-11"
          >
            {all ? "Show fewer" : `Show all ${group.rows.length}`}
          </button>
        </li>
      )}
    </Section>
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

/** «Projects»: a group a project, in the tree's order, «No project» last. */
export function ProjectList({ plan, groups, today, projects }: { plan: Plan; groups: TaskGroup[]; today: string; projects: Projects }) {
  return groups.map((g) => <ProjectGroup key={g.key || "none"} plan={plan} group={g} today={today} projects={projects} />);
}

type Grouping = "projects" | "days";

const GROUPINGS: [Grouping, string][] = [
  ["projects", "Projects"],
  ["days", "Days"],
];

/** The projects of the Base for the rows: its tree in the left panel's order, where a task is, and its words. */
function useTaskProjects(): Projects {
  const spheres = live.meta?.spheres ?? NO_SPHERES;
  const nodes = useProjects();
  const of = useProjectOf(spheres);
  return useMemo(
    () => ({
      tree: projectTree(nodes, spheres),
      of: (t: Task) => (t.fields.project ? of({ project: t.fields.project }) : ""),
      label: (key: string) => projectLabel(key, nodes, spheres),
      name: (key: string) => projectName(key, nodes, spheres),
    }),
    [nodes, spheres, of],
  );
}

export function TasksTab({ plan, today }: { plan: Plan; today: string }) {
  const [by, setBy] = useState<Grouping>("projects");
  const projects = useTaskProjects();
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
  // in a project, what has no day first, as «To plan» has them, then the planned, soonest first
  const groups = useMemo(() => byProject([...unplanned, ...planned], projects.of, projects.tree), [unplanned, planned, projects]);
  const labelOf = (t: Task) => {
    const key = projects.of(t);
    return key ? projects.label(key) : undefined;
  };
  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 px-4 py-5 md:px-6">
      <div className="flex flex-wrap items-center gap-3">
        <fieldset aria-label="Group the tasks" className="inline-flex h-9 rounded-md border p-0.5 max-md:h-11">
          {GROUPINGS.map(([g, words]) => (
            <button
              key={g}
              type="button"
              aria-pressed={by === g}
              data-grouping={g}
              onClick={() => setBy(g)}
              className={cn("rounded px-3 text-[13px] transition-colors", by === g ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground")}
            >
              {words}
            </button>
          ))}
        </fieldset>
        <span className="text-xs text-muted-foreground tabular-nums">
          {open.length} open · {unplanned.length} without a day{doneToday.length ? ` · ${doneToday.length} done today` : ""}
        </span>
      </div>
      <AddTask />
      <FromPeople plan={plan} today={today} />
      {by === "projects" ? (
        <ProjectList plan={plan} groups={groups} today={today} projects={projects} />
      ) : (
        <>
          {unplanned.length > 0 && (
            <Section title="To plan" count={unplanned.length} hint="pick a day for each">
              {unplanned.map((t) => <TaskRow key={t.id} plan={plan} task={t} today={today} projects={projects} project={labelOf(t)} />)}
            </Section>
          )}
          {planned.length > 0 && (
            <Section title="Planned" count={planned.length} hint="soonest first">
              {planned.map((t) => <TaskRow key={t.id} plan={plan} task={t} today={today} projects={projects} project={labelOf(t)} />)}
            </Section>
          )}
        </>
      )}
      {doneToday.length > 0 && (
        <Section title="Done today" count={doneToday.length}>
          {doneToday.map((t) => <TaskRow key={t.id} plan={plan} task={t} today={today} project={labelOf(t)} />)}
        </Section>
      )}
      {!open.length && !doneToday.length && (
        <p className="px-1 text-sm text-muted-foreground">No open tasks. Add one above, or tell the assistant in Telegram what to do.</p>
      )}
    </div>
  );
}
