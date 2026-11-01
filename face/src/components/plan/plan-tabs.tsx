// «Today»'s tabs beside «Waiting»: Tasks, Day, Week — one read of the plan for the three, and the views' editor
// around them, so a task's record opens in the same drawer as any record's and every write has its «Undo».
import { Editor } from "@/components/views/edit";
import { dayOf, type Plan } from "@/lib/plan";
import { DayTab } from "./day-tab";
import { TasksTab } from "./tasks-tab";
import { WeekTab } from "./week-tab";

export type PlanTab = "tasks" | "day" | "week";

export function PlanTabs({ tab, plan, error, reload }: { tab: PlanTab; plan: Plan | null; error: string | null; reload: () => void }) {
  if (!plan) {
    return (
      <p className="px-4 py-6 text-sm text-muted-foreground md:px-6">
        {error ? `Not loaded: ${error}` : "Loading…"}
      </p>
    );
  }
  if (plan.tasks.missing) {
    return (
      <div className="mx-auto flex max-w-2xl flex-col gap-2 px-4 py-6 text-sm text-muted-foreground md:px-6">
        <p className="text-foreground">No tasks collection yet.</p>
        <p>
          It is set up once from the server's terminal: <code className="font-mono text-xs">retinue-platform collections define tasks …</code>
          and its rights — see docs/collections.md, «Tasks».
        </p>
      </div>
    );
  }
  const today = dayOf(plan.now, plan.tz);
  return (
    <Editor today={today} refresh={() => reload()} reload={reload}>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === "tasks" && <TasksTab plan={plan} today={today} reload={reload} />}
        {tab === "day" && <DayTab plan={plan} today={today} reload={reload} />}
        {tab === "week" && <WeekTab plan={plan} today={today} />}
      </div>
    </Editor>
  );
}
