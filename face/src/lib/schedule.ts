// The owner's schedule from «Today» (docs/face.md «Tasks and Schedule»): his verbs on his tasks and his time, each
// taken the way it goes. A task's fields and a block he puts, moves or stretches are his writes of `tasks` and `blocks`
// through the views' editor — at the version shown, with their «Undo» and their question when the record moved on —
// and such a block is always his own, `locked`, so no plan moves it; what lays, takes off or holds time is a press the
// Router does (`/api/plan/press`), said in a toast once sent. Taking a block off is a press, not a write: the Router
// keeps the plan's own account of it.
import type { Step } from "./edit";
import { type Activity, type Block, iso, planApi, type PlanWindow, type Press, type Task, type TaskFields } from "./plan";
import { toast } from "./toast";

/** The editor's write (`Editing.write`): true once written — after the owner's answer, when it was asked. */
export type Write = (collection: string, steps: Step[], what: string) => Promise<boolean>;

/** Every verb is true once done; `what` is its toast. */
export interface Schedule {
  /** A new open task. */
  add(fields: TaskFields, what: string): Promise<boolean>;
  /** A task's fields; null takes one off. */
  set(task: Task, fields: { [K in keyof TaskFields]?: TaskFields[K] | null }, what: string): Promise<boolean>;
  /** A block of his at an exact time. */
  put(task: Task, start: number, minutes: number, what: string): Promise<boolean>;
  move(block: Block, start: number, what: string): Promise<boolean>;
  resize(block: Block, minutes: number, what: string): Promise<boolean>;
  /** A block taken off the plan. */
  remove(block: Block, what: string): Promise<boolean>;
  /** «Plan my day», or seven windows from that day's. */
  plan(day: string, days: 1 | 7, what: string): Promise<boolean>;
  /** One task to a day — or a person's row (its token) or a reminder (its id), the Router making its task first. */
  place(day: string, of: Task | { t: string } | { ref: string }, what: string): Promise<boolean>;
  undo(plan: string, what: string): Promise<boolean>;
  busy(minutes: 30 | 60 | 120, what: string): Promise<boolean>;
  /** One of his activities from now. */
  now(activity: string, what: string): Promise<boolean>;
  /** The hold running now ends. */
  free(what: string): Promise<boolean>;
  /** «Not working today». */
  off(what: string): Promise<boolean>;
  hours(window: PlanWindow, what: string): Promise<boolean>;
  activities(activities: Activity[], what: string): Promise<boolean>;
}

/** The schedule over an editor's write (none where the views are read only: his writes do nothing) and the plan's
 * presses. */
export function schedule(write: Write | null, send: (body: Press) => Promise<unknown> = (body) => planApi.press(body)): Schedule {
  const tasks = (steps: Step[], what: string) => (write ? write("tasks", steps, what) : Promise.resolve(false));
  const blocks = (steps: Step[], what: string) => (write ? write("blocks", steps, what) : Promise.resolve(false));
  const own = (block: Block, fields: Partial<Block["fields"]>, what: string) =>
    blocks([{ op: { op: "set", id: block.id, version: block.version, fields: { ...fields, locked: true } }, target: block.id }], what);
  const press = async (body: Press, what: string) => {
    try {
      await send(body);
      toast(what);
      return true;
    } catch (e) {
      toast(e instanceof Error ? e.message : "It did not go through.", "error", 6000);
      return false;
    }
  };
  return {
    add: (fields, what) => tasks([{ op: { op: "add", fields: { state: "open", ...fields } }, target: null }], what),
    set: (task, fields, what) => tasks([{ op: { op: "set", id: task.id, version: task.version, fields }, target: task.id }], what),
    put: (task, start, minutes, what) =>
      blocks([{ op: { op: "add", fields: { task: task.id, start: iso(start), minutes, state: "planned", locked: true } }, target: null }], what),
    move: (block, start, what) => own(block, { start: iso(start) }, what),
    resize: (block, minutes, what) => own(block, { minutes }, what),
    remove: (block, what) => press({ action: "remove", block: block.id }, what),
    plan: (day, days, what) => press(days === 7 ? { action: "plan", day, days } : { action: "plan", day }, what),
    place: (day, of, what) => press({ action: "place", day, ...("id" in of ? { task: of.id } : of) }, what),
    undo: (plan, what) => press({ action: "undo", plan }, what),
    busy: (minutes, what) => press({ action: "busy", minutes }, what),
    now: (activity, what) => press({ action: "now", activity }, what),
    free: (what) => press({ action: "free" }, what),
    off: (what) => press({ action: "off" }, what),
    hours: (window, what) => press({ action: "hours", ...window }, what),
    activities: (activities, what) => press({ action: "activities", activities }, what),
  };
}
