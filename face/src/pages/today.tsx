// `/today`, the home screen: what waits for the owner and what goes on, as an inbox (`components/inbox.tsx`) — the
// list at the left, the row opened whole at the right — under a header of one line: the day, how many wait, and the
// hub's status, of which only what is off shows in words. Everything comes from what the app already reads: the
// session rows, the stream, the feed.
import { Link } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { AppHeader } from "@/components/app-header";
import { Inbox } from "@/components/inbox";
import { type PlanTab, PlanTabs } from "@/components/plan/plan-tabs";
import { Chart } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { type Chip, type Feed, useFeed } from "@/lib/feed";
import { live, useLiveState } from "@/lib/live";
import { useWide } from "@/lib/media";
import { type Plan, usePlan } from "@/lib/plan";
import { listed } from "@/lib/store";
import { finished, running, todayWord, useSince } from "@/lib/today";
import type { Session, Sphere } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Running sessions whose transcript the app has not loaded: their snapshot is read once, then the stream keeps it. */
const WATCH_AT_MOST = 5;

function useNow(everyMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), everyMs);
    return () => clearInterval(timer);
  }, [everyMs]);
  return now;
}

const OFF: Record<string, string> = { warn: "text-foreground", bad: "text-destructive" };

/** The hub's status: what is fine is one dot (its words in the tooltip), what is off is said. */
export function Status({ chips }: { chips: Chip[] }) {
  if (!chips.length) return null;
  const fine = chips.filter((c) => c.level === "ok");
  const off = chips.filter((c) => c.level !== "ok");
  return (
    <ul aria-label="Status" className="flex min-w-0 items-center gap-4 text-xs text-muted-foreground">
      {fine.length > 0 && (
        <li title={fine.map((c) => c.text).join(" · ")} className="flex items-center gap-1.5">
          <span aria-hidden className="size-1.5 rounded-full bg-success" />
          <span className={cn(off.length && "sr-only")}>{fine.map((c) => c.text).join(" · ")}</span>
        </li>
      )}
      {off.map((c) => (
        <li key={c.key} className={cn("truncate", OFF[c.level] ?? "")}>
          {c.text}
        </li>
      ))}
    </ul>
  );
}

export type TodayTab = "waiting" | PlanTab;
const TABS: [TodayTab, string][] = [["waiting", "Waiting"], ["tasks", "Tasks"], ["day", "Day"], ["week", "Week"]];

/** The header's tabs: «Waiting» (the queue), and the tasks, the day and the week. */
function Tabs({ tab, onTab, counts }: { tab: TodayTab; onTab: (t: TodayTab) => void; counts: Partial<Record<TodayTab, number>> }) {
  return (
    <div role="tablist" aria-label="Today" className="flex shrink-0 items-stretch self-stretch">
      {TABS.map(([value, label]) => (
        <button
          key={value}
          type="button"
          role="tab"
          aria-selected={tab === value}
          onClick={() => onTab(value)}
          className={cn(
            "-mb-px inline-flex items-center gap-1.5 border-b-2 px-2.5 text-[13px] whitespace-nowrap transition-colors max-sm:px-2",
            tab === value ? "border-foreground text-foreground" : "border-transparent text-muted-foreground hover:text-foreground",
          )}
        >
          {label}
          {!!counts[value] && <span className="font-mono text-xs text-muted-foreground tabular-nums">{counts[value]}</span>}
        </button>
      ))}
    </div>
  );
}

export function TodayView({
  feed,
  sessions,
  spheres,
  since,
  now,
  tab = "waiting",
  onTab,
  plan = null,
}: {
  feed: Feed | null;
  sessions: Session[];
  spheres: Sphere[];
  since: number;
  now: number;
  tab?: TodayTab;
  onTab?: (t: TodayTab) => void;
  plan?: { plan: Plan | null; error: string | null; reload: () => void } | null;
}) {
  const today = new Date(now * 1000);
  const busy = useMemo(() => running(sessions), [sessions]);
  const done = useMemo(() => finished(sessions, since), [sessions, since]);
  const ids = busy.map((s) => s.id).join(",");
  // biome-ignore lint/correctness/useExhaustiveDependencies: keyed on the ids, not on every new row object
  useEffect(() => {
    for (const s of busy.slice(0, WATCH_AT_MOST))
      if (!(s.id in live.getState().floors) && !live.isWatched(s.id)) live.watch(s.id).catch(() => undefined);
  }, [ids]);
  // the same number as the count in the sidebar and the header: what the feed says waits
  const waiting = feed?.count ?? 0;
  const open = plan?.plan?.tasks.rows.filter((t) => t.fields.state === "open").length ?? 0;
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b px-4 md:px-6">
        <h1 className={cn("text-[15px] font-medium", onTab && "max-sm:sr-only")}>Today</h1>
        <span className="text-sm whitespace-nowrap text-muted-foreground max-lg:hidden">{todayWord(today)}</span>
        {onTab ? (
          <Tabs tab={tab} onTab={onTab} counts={{ waiting, tasks: open }} />
        ) : (
          feed && waiting > 0 && <span className="text-sm whitespace-nowrap text-muted-foreground tabular-nums">· {waiting} waiting</span>
        )}
        <div className="ml-auto flex min-w-0 items-center gap-3">
          <Status chips={feed?.chips ?? []} />
          {/* the phone's way to «Week»: the tab bar has no room for it; the sidebar has it on a wide screen */}
          <Link
            to="/week"
            aria-label="The week in numbers"
            className="hit inline-flex shrink-0 items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground lg:hidden"
          >
            <Chart className="size-4" />
            {!onTab && "Week"}
          </Link>
        </div>
      </header>
      {tab === "waiting" || !plan ? (
        <Inbox feed={feed} running={busy} done={done} spheres={spheres} now={now} />
      ) : (
        <PlanTabs tab={tab} plan={plan.plan} error={plan.error} reload={plan.reload} />
      )}
    </div>
  );
}

export function TodayPage({ tab = "waiting", onTab }: { tab?: TodayTab; onTab?: (t: TodayTab) => void }) {
  const feed = useFeed();
  const { sessions } = useLiveState();
  const since = useSince();
  const now = useNow();
  const list = useMemo(() => listed(sessions), [sessions]);
  const wide = useWide();
  const plan = usePlan();
  return (
    <div className="flex h-full min-h-0 flex-col">
      {!wide && <AppHeader />}
      <main className="flex min-h-0 flex-1 flex-col">
        <TodayView feed={feed} sessions={list} spheres={live.meta?.spheres ?? []} since={since} now={now} tab={tab} onTab={onTab} plan={plan} />
      </main>
    </div>
  );
}

/** The count on the tab and in the sidebar: what waits for the owner. */
export function TodayCount({ className }: { className?: string }) {
  const feed = useFeed();
  if (!feed || feed.count === 0) return null;
  return (
    <Badge variant="warning" className={className} aria-label={`${feed.count} waiting`}>
      {feed.count}
    </Badge>
  );
}
