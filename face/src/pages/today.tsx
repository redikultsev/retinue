// `/today`, the home screen: «Waiting for you» — the feed, grouped by action — then «Working» (each running
// session with its sphere, title, last line and how long it runs), «Done» (finished since the owner last had the
// app open, not read yet), «Deadlines» (the base's dates of the week, from the router's digest) and the hub's status
// chips. Everything comes from what the app already reads: the session rows, the stream, the feed.
import { PageHeader, PAGE_X } from "@/components/page-header";
import { Link } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { AppHeader } from "@/components/app-header";
import { FeedView } from "@/components/feed";
import { Chart } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { type Chip, type Deadline, type Feed, useFeed } from "@/lib/feed";
import { when } from "@/lib/format";
import { useWide } from "@/lib/media";
import { live, useLiveState } from "@/lib/live";
import { splitRecord, useOpenRecord } from "@/lib/pane";
import { sphereLabel } from "@/lib/spheres";
import { listed, orderedItems } from "@/lib/store";
import { dayWords, elapsed, finished, lastLine, running, tickMs, todayWord, turnStart, useSince } from "@/lib/today";
import type { Session, Sphere } from "@/lib/types";
import { cn, LIST, LIST_ROW } from "@/lib/utils";

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

function Section({ title, count, children }: { title: string; count?: number; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="flex flex-col gap-2">
      <h2 className="px-1 text-base font-semibold">
        {title} {count ? <span className="tabular-nums">({count})</span> : null}
      </h2>
      {children}
    </section>
  );
}

function where(s: Session, spheres: Sphere[]): string {
  const sphere = spheres.find((x) => x.name === s.sphere);
  return sphere ? sphereLabel(sphere) : s.sphere || s.track;
}

function Running({ sessions, spheres, now: given }: { sessions: Session[]; spheres: Sphere[]; now: number }) {
  const state = useLiveState();
  // its own clock: the seconds of a fresh turn count, then every half minute
  const [now, setNow] = useState(given);
  useEffect(() => setNow(given), [given]);
  const every = tickMs(sessions, now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), every);
    return () => clearInterval(timer);
  }, [every]);
  const ids = sessions.map((s) => s.id).join(",");
  // biome-ignore lint/correctness/useExhaustiveDependencies: keyed on the ids, not on every new row object
  useEffect(() => {
    for (const s of sessions.slice(0, WATCH_AT_MOST))
      if (!(s.id in live.getState().floors) && !live.isWatched(s.id)) live.watch(s.id).catch(() => undefined);
  }, [ids]);
  return (
    <ul className={LIST}>
      {sessions.map((s) => {
        const line = lastLine(orderedItems(state.items[s.id]), state.deltas[s.id]);
        return (
          <li key={s.id}>
            <Link
              to="/s/$id"
              params={{ id: s.id }}
              data-testid="running-row"
              className={cn(LIST_ROW, "flex-col gap-0.5")}
            >
              <span className="flex min-w-0 items-baseline gap-2 text-[15px] md:text-sm">
                <span className="size-2 shrink-0 animate-pulse self-center rounded-full bg-primary" />
                <span className="min-w-0 truncate font-medium">{s.title || s.track}</span>
                <span className="hidden min-w-0 shrink truncate text-xs text-muted-foreground sm:inline">{where(s, spheres)}</span>
                <span className="ml-auto shrink-0 text-xs text-muted-foreground tabular-nums">{elapsed(turnStart(s), now)}</span>
              </span>
              <span className="truncate pl-4 text-sm text-muted-foreground">{line || "thinking…"}</span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

function Done({ sessions, spheres, now }: { sessions: Session[]; spheres: Sphere[]; now: number }) {
  return (
    <ul className={LIST}>
      {sessions.map((s) => (
        <li key={s.id}>
          <Link
            to="/s/$id"
            params={{ id: s.id }}
            data-testid="done-row"
            className={cn(LIST_ROW, "min-h-12 items-center gap-2")}
          >
            <span role="img" aria-label="unread" className="size-2.5 shrink-0 rounded-full bg-primary" />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[15px] font-medium md:text-sm">{s.title || s.track}</span>
              <span className="block truncate text-xs text-muted-foreground">{where(s, spheres)}</span>
            </span>
            <span className="shrink-0 text-xs text-muted-foreground">{when(s.at, now)}</span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function Deadlines({ items, today }: { items: Deadline[]; today: Date }) {
  const openRecord = useOpenRecord();
  return (
    <ul className={LIST}>
      {items.map((d) => {
        const { date, overdue } = dayWords(d.when, today);
        const record = d.path ? splitRecord(d.path) : null;
        const body = (
          <>
            <span className={cn("w-16 shrink-0 text-xs tabular-nums", overdue ? "text-destructive" : "text-muted-foreground")}>{date}</span>
            <span className="min-w-0 flex-1">
              <span className="block text-sm break-words">{d.title}</span>
              {(d.sphere_title || d.sphere) && (
                <span className="block truncate text-xs text-muted-foreground">
                  {d.sphere_icon ? `${d.sphere_icon} ` : ""}
                  {d.sphere_title || d.sphere}
                </span>
              )}
            </span>
          </>
        );
        return (
          <li key={d.id} data-testid="deadline-row">
            {record ? (
              <button
                type="button"
                onClick={() => openRecord(record.space, record.path)}
                className={cn(LIST_ROW, "items-start gap-2")}
              >
                {body}
              </button>
            ) : (
              <div className="flex items-start gap-2 px-3 py-2.5">{body}</div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

const CHIP_TONES: Record<string, string> = {
  ok: "border-success/40 text-foreground",
  warn: "border-warning bg-warning/15",
  bad: "border-destructive/60 bg-destructive/10 text-destructive",
};

export function Chips({ chips }: { chips: Chip[] }) {
  if (!chips.length) return null;
  return (
    <ul aria-label="Status" className="flex flex-wrap gap-1.5">
      {chips.map((c) => (
        <li key={c.key} className={cn("rounded-full border px-2.5 py-0.5 text-xs", CHIP_TONES[c.level] ?? "")}>
          {c.text}
        </li>
      ))}
    </ul>
  );
}

export function TodayView({
  feed,
  sessions,
  spheres,
  since,
  now,
}: {
  feed: Feed | null;
  sessions: Session[];
  spheres: Sphere[];
  since: number;
  now: number;
}) {
  const today = new Date(now * 1000);
  const busy = useMemo(() => running(sessions), [sessions]);
  const done = useMemo(() => finished(sessions, since), [sessions, since]);
  const doneIds = useMemo(() => new Set(done.map((s) => s.id)), [done]);
  const deadlines = feed?.deadlines ?? [];
  const deadlineIds = useMemo(() => new Set(deadlines.map((d) => d.id)), [deadlines]);
  return (
    <>
    <PageHeader
      title={`Today · ${todayWord(today)}`}
      actions={
        // the phone's way to «Week»: the tab bar has no room for it; the sidebar has it on a wide screen
        <Link to="/week" className="inline-flex min-h-9 items-center gap-1.5 rounded-md px-2 text-sm text-muted-foreground hover:bg-accent hover:text-foreground lg:hidden">
          <Chart className="size-4" />
          Week
        </Link>
      }
    >
      <Chips chips={feed?.chips ?? []} />
    </PageHeader>
    <div className={cn("mx-auto flex w-full max-w-3xl flex-col gap-5 pt-4 pb-6", PAGE_X)}>
      <FeedView
        feed={feed}
        now={now}
        heading="h2"
        empty="Nothing is waiting for you."
        // each thing once: what finished since he last looked is under «Done», a base's date under «Deadlines»
        skip={(row) =>
          (row.action === "read" && !!row.session && doneIds.has(row.session)) ||
          (row.action === "assistant" && row.source === "deadline" && deadlineIds.has(row.ref))
        }
      />
      {busy.length > 0 && (
        <Section title="Working" count={busy.length}>
          <Running sessions={busy} spheres={spheres} now={now} />
        </Section>
      )}
      {done.length > 0 && (
        <Section title="Done" count={done.length}>
          <Done sessions={done} spheres={spheres} now={now} />
        </Section>
      )}
      {deadlines.length > 0 && (
        <Section title="Deadlines" count={deadlines.length}>
          <Deadlines items={deadlines} today={today} />
        </Section>
      )}
    </div>
    </>
  );
}

export function TodayPage() {
  const feed = useFeed();
  const { sessions } = useLiveState();
  const since = useSince();
  const now = useNow();
  const list = useMemo(() => listed(sessions), [sessions]);
  const wide = useWide();
  return (
    <div className="flex h-full min-h-0 flex-col">
      {!wide && <AppHeader />}
      <main className="min-h-0 flex-1 overflow-y-auto">
        <TodayView feed={feed} sessions={list} spheres={live.meta?.spheres ?? []} since={since} now={now} />
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
