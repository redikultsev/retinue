// «Today»'s tabs beside what waits: «Tasks», and «Schedule» — the week as an hourly grid (a day on a phone). One read
// of the plan for both, and the views' editor around them, so a task's record opens in the same drawer as any
// record's and every write has its «Undo».
import { lazy, Suspense } from "react";
import { Editor } from "@/components/views/edit";
import { dayOf, type Plan } from "@/lib/plan";
import { TasksTab } from "./tasks-tab";

// The grid is FullCalendar, ~80 kB gzipped: its own chunk, fetched when the tab opens.
const GridTab = lazy(() => import("./grid-tab").then((m) => ({ default: m.GridTab })));

export type PlanTab = "tasks" | "schedule";

export function PlanTabs({
  tab,
  plan,
  error,
  reload,
}: {
  tab: PlanTab;
  plan: Plan | null;
  error: string | null;
  reload: () => void;
}) {
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
        {tab === "tasks" && <TasksTab plan={plan} today={today} />}
        {tab === "schedule" && (
          <Suspense fallback={<p className="px-4 py-6 text-sm text-muted-foreground md:px-6">Loading the grid…</p>}>
            <GridTab plan={plan} today={today} />
          </Suspense>
        )}
      </div>
    </Editor>
  );
}
