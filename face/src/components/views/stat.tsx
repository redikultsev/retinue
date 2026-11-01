// The numbers: on a tab of their own, a tile per `stat` view, two to a row on a phone, four on a wide screen — each its
// number, its goal (as a bar and in words), its window and the period before; beside the work, a grid of figures.
import { type CollectionViews, type Placed, previousWords, progress, type StatView, windowWords } from "@/lib/collections";
import { day } from "@/lib/spaces";
import { cn } from "@/lib/utils";

/** The number against its goal: a bar filled to the share, a tick at the goal. Its words are in `aria-label`, and
 * «met» is said in the tile as well as shown by the colour. */
function GoalBar({ value, goal, share, met }: { value: number; goal: number; share: number; met: boolean }) {
  return (
    <svg viewBox="0 0 100 6" preserveAspectRatio="none" className="h-1.5 w-full" role="img" aria-label={`${value} of ${goal}${met ? ", goal met" : ""}`}>
      <rect x={0} y={0} width={100} height={6} rx={3} className="fill-muted-foreground/15" />
      {share > 0 && <rect x={0} y={0} width={Math.max(6, share * 100)} height={6} rx={3} className={met ? "fill-success" : "fill-foreground/70"} />}
    </svg>
  );
}

function Tile({ collection, view }: { collection: CollectionViews; view: StatView }) {
  const goal = progress(view);
  const before = previousWords(view);
  const none = collection.rev === null;
  return (
    <li className="flex min-w-0 flex-col gap-1 rounded-xl border bg-card px-4 py-3" data-testid={`stat-${view.name}`}>
      <span className="line-clamp-2 text-[13px] leading-snug text-muted-foreground">{view.title}</span>
      <span className="flex items-baseline gap-1">
        <span className={cn("font-mono text-3xl leading-9 font-medium tabular-nums", none && "text-muted-foreground")}>{none ? "—" : view.value}</span>
        {view.goal !== null && <span className="text-sm text-muted-foreground tabular-nums">of {view.goal}</span>}
        {goal?.met && <span className="ml-auto text-xs font-medium text-success">met</span>}
      </span>
      {goal && view.goal !== null && !none && <GoalBar value={view.value} goal={view.goal} share={goal.share} met={goal.met} />}
      <span className="text-xs text-muted-foreground">{none ? "No data yet" : windowWords(view)}</span>
      {before && !none && <span className="text-xs text-muted-foreground tabular-nums">{before}</span>}
    </li>
  );
}

function Broken({ title, error }: { title: string; error: string }) {
  return (
    <li className="flex min-w-0 flex-col gap-1 rounded-xl border border-dashed p-3">
      <span className="line-clamp-2 text-xs leading-snug text-muted-foreground">{title}</span>
      <span className="text-xs break-words text-muted-foreground">Not counted: {error}</span>
    </li>
  );
}

export function StatStrip({ stats }: { stats: Placed[] }) {
  return (
    <ul className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {stats.map(({ collection, view }) =>
        view.error !== undefined ? (
          <Broken key={`${collection.name}/${view.name}`} title={view.title} error={view.error} />
        ) : view.kind === "stat" ? (
          <Tile key={`${collection.name}/${view.name}`} collection={collection} view={view} />
        ) : null,
      )}
    </ul>
  );
}

/** The numbers beside the work, two to a row: each its figure — against its goal, «0 / 7» — and its title under it. */
export function StatList({ stats }: { stats: Placed[] }) {
  return (
    <dl className="grid grid-cols-2 overflow-hidden rounded-lg border bg-card text-[13px] [&>div:nth-child(n+3)]:border-t [&>div:nth-child(odd)]:border-r" data-testid="stat-list">
      {stats.map(({ collection, view }) => {
        const key = `${collection.name}/${view.name}`;
        if (view.error !== undefined || view.kind !== "stat")
          return (
            <div key={key} className="flex min-w-0 flex-col-reverse gap-0.5 px-3 py-2.5 text-muted-foreground">
              <dt className="truncate text-xs" title={view.error}>{`${view.title}: not counted`}</dt>
              <dd className="font-mono text-lg leading-6">—</dd>
            </div>
          );
        const none = collection.rev === null;
        return (
          <div key={key} className="flex min-w-0 flex-col-reverse gap-0.5 px-3 py-2.5" data-testid={`stat-${view.name}`}>
            <dt className="truncate text-xs text-muted-foreground" title={windowWords(view)}>
              {view.title}
            </dt>
            <dd className={cn("font-mono text-lg leading-6 tabular-nums", none && "text-muted-foreground")}>
              {none ? "—" : view.value}
              {!none && view.goal !== null && <span className="text-xs text-muted-foreground">{` / ${view.goal}`}</span>}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

/** What the numbers count, said once over them: «This week» and its days when all count the same window. */
export function statsWindow(stats: Placed[]): { title: string; days: string } {
  const views = stats.flatMap(({ view }) => (view.error === undefined && view.kind === "stat" ? [view] : []));
  const one = views[0];
  if (!one?.window || !one.first || !one.last || views.some((v) => v.window !== one.window || v.first !== one.first)) return { title: "Numbers", days: "" };
  return { title: `This ${one.window}`, days: `${day(one.first)} – ${day(one.last)}` };
}
