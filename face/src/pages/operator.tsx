// `/operator`, «Operator»: the sessions' requests alone, a place of its own beside «Today» — as cards answered in place
// (components/operator.tsx), or as the map of the sessions in their places (components/map.tsx), the pick kept on the
// device. The room's sessions and the owner's computers' alike: a computer's is answered on that computer.
import { useEffect, useState } from "react";
import { AppHeader } from "@/components/app-header";
import { MapView } from "@/components/map";
import { Operator, OperatorSwitch } from "@/components/operator";
import { useComputers } from "@/lib/computers";
import { type Feed, useFeed } from "@/lib/feed";
import { live } from "@/lib/live";
import { useWide } from "@/lib/media";
import { keepOperatorView, keptOperatorView, type OperatorView } from "@/lib/operator";
import { requests, waits } from "@/lib/waiting";

function useNow(everyMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), everyMs);
    return () => clearInterval(timer);
  }, [everyMs]);
  return now;
}

/** Why the cards are none: what was looked at, so an empty screen says whether that is right. */
export function emptyWhy(feed: Feed | null, computers: { off: boolean; sessions: unknown[] }[]): string {
  if (!feed) return "Loading…";
  const on = computers.filter((c) => !c.off);
  const listed = on.reduce((n, c) => n + c.sessions.length, 0);
  if (computers.length && !on.length) return "No session is asking you for anything. Your computer has not reported for a while: it may be asleep.";
  if (on.length) return `No session is asking you for anything. Your computer lists ${listed} session${listed === 1 ? "" : "s"}, none waiting on you.`;
  return "No session is asking you for anything.";
}

export function OperatorPage({ view: asked }: { view?: OperatorView }) {
  const feed = useFeed();
  const computers = useComputers();
  const now = useNow();
  const wide = useWide();
  const spheres = live.meta?.spheres ?? [];
  const [view, setView] = useState<OperatorView>(() => {
    if (asked) keepOperatorView(asked);
    return asked ?? keptOperatorView();
  });
  const pick = (v: OperatorView) => {
    keepOperatorView(v);
    setView(v);
  };
  const waiting = requests(feed).filter(waits).length;
  const head = <OperatorSwitch view={view} onView={pick} />;
  return (
    <div className="flex h-full min-h-0 flex-col">
      {!wide && <AppHeader />}
      <main className="flex min-h-0 flex-1 flex-col">
        <header className="flex h-12 shrink-0 items-center gap-2 border-b px-4 md:px-6">
          <h1 className="text-[15px] font-medium">Operator</h1>
          {waiting > 0 && <span className="text-sm whitespace-nowrap text-muted-foreground tabular-nums">· {waiting} waiting</span>}
        </header>
        {view === "map" ? (
          <MapView feed={feed} spheres={spheres} now={now} head={head} />
        ) : (
          <Operator feed={feed} spheres={spheres} now={now} head={head} empty={emptyWhy(feed, computers)} />
        )}
      </main>
    </div>
  );
}

/** The count beside «Operator»: the requests that wait for the owner — the cards' numbers go to these. */
export function useOperatorCount(): number {
  const feed = useFeed();
  return requests(feed).filter(waits).length;
}
