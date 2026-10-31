// `/week`, «Week»: how the work went — the platform's tally of the last eight seven-day spans (`GET /api/stats`,
// signals.tally). A row of tiles, each this week's number, the change from the week before and an eight-week line;
// under them how the gate answered, how the cards ended and which tools failed. The screen compares and draws; it
// never counts. Gate and tool-error counts start with the platform's journal, so weeks before it read zero.
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { ApiError, api } from "@/lib/api";
import type { Tally } from "@/lib/types";
import { cn } from "@/lib/utils";

const day = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });

/** «24 Oct – 31 Oct». */
export function spanLabel(t: Tally): string {
  return `${day.format(new Date(t.since * 1000))} – ${day.format(new Date(t.until * 1000))}`;
}

/** The change from the week before, as said under a tile: «+3 vs last week», «same as last week». */
export function delta(now: number, before: number | undefined): string {
  if (before === undefined) return "";
  const d = now - before;
  return d === 0 ? "same as last week" : `${d > 0 ? "+" : "−"}${Math.abs(d)} vs last week`;
}

interface Metric {
  key: string;
  label: string;
  value: (t: Tally) => number;
  /** More is worse: its rise is marked. */
  bad?: boolean;
}

export const METRICS: Metric[] = [
  { key: "sessions", label: "Sessions", value: (t) => t.sessions + t.sub_sessions },
  { key: "runs", label: "Runs", value: (t) => t.runs },
  { key: "messages", label: "Your messages", value: (t) => t.owner.messages },
  { key: "cards", label: "Approval cards", value: (t) => t.cards.approval.shown },
  { key: "gate", label: "Gate answers", value: (t) => t.gate.calls },
  { key: "errors", label: "Tool errors", value: (t) => t.engine.tool_errors, bad: true },
];

/** Eight weeks as a line, the oldest on the left; this week's point marked. Each point says its week on hover. */
function Sparkline({ weeks, value }: { weeks: Tally[]; value: (t: Tally) => number }) {
  const points = [...weeks].reverse();
  const values = points.map(value);
  const max = Math.max(1, ...values);
  const w = 112;
  const h = 28;
  const step = points.length > 1 ? (w - 8) / (points.length - 1) : 0;
  const x = (i: number) => 4 + i * step;
  const y = (v: number) => h - 4 - (v / max) * (h - 8);
  const path = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="h-7 w-28 overflow-visible" role="img" aria-label="Eight weeks">
      <path d={path} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className="text-primary" />
      {values.map((v, i) => (
        <g key={i}>
          <circle cx={x(i)} cy={y(v)} r={8} fill="transparent">
            <title>{`${points[i] ? spanLabel(points[i]) : ""}: ${v}`}</title>
          </circle>
          {i === values.length - 1 && <circle cx={x(i)} cy={y(v)} r={3} className="fill-primary stroke-background" strokeWidth={2} />}
        </g>
      ))}
    </svg>
  );
}

function Tile({ metric, weeks, current }: { metric: Metric; weeks: Tally[]; current: Tally }) {
  const now = metric.value(current);
  const before = weeks[1] ? metric.value(weeks[1]) : undefined;
  const worse = metric.bad && before !== undefined && now > before;
  return (
    <li data-testid="week-tile" className="flex flex-col gap-1 rounded-lg border bg-card p-3">
      <span className="text-xs text-muted-foreground">{metric.label}</span>
      <span className="text-2xl font-semibold tabular-nums">{now}</span>
      <span className={cn("text-xs tabular-nums", worse ? "font-medium text-destructive" : "text-muted-foreground")}>
        {worse && <span aria-hidden="true">▲ </span>}
        {delta(now, before)}
      </span>
      <Sparkline weeks={weeks} value={metric.value} />
    </li>
  );
}

/** Labelled counts as one-hue bars, the largest first: magnitude, never identity. */
function Bars({ rows }: { rows: [string, number][] }) {
  const shown = rows.filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
  if (shown.length === 0) return <p className="text-sm text-muted-foreground">Nothing this week.</p>;
  const max = Math.max(...shown.map(([, n]) => n));
  return (
    <ul className="flex flex-col gap-1.5">
      {shown.map(([label, n]) => (
        <li key={label} title={`${label}: ${n}`} className="grid grid-cols-[minmax(0,12rem)_1fr_3rem] items-center gap-2 text-sm">
          <span className="truncate">{label}</span>
          <span className="h-2 rounded-r bg-primary/80" style={{ width: `${Math.max(2, (n / max) * 100)}%` }} />
          <span className="text-right tabular-nums text-muted-foreground">{n}</span>
        </li>
      ))}
    </ul>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="flex flex-col gap-2">
      <h2 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">{title}</h2>
      {children}
    </section>
  );
}

const GATE: [string, string][] = [
  ["rule", "By a rule"],
  ["rule_deny", "Denied by a rule"],
  ["classifier", "Left to the classifier"],
  ["blocked", "Stopped by the classifier"],
  ["defer", "Sent to you"],
  ["allow", "Granted by you"],
  ["deny", "Refused by the gate"],
  ["asked", "Asked (fallback run)"],
];

export function WeekView({ weeks, error, onRetry }: { weeks: Tally[] | null; error?: string | null; onRetry?: () => void }) {
  const t = weeks?.[0];
  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      <header className="flex shrink-0 flex-col gap-0.5 border-b bg-background px-4 pt-[calc(env(safe-area-inset-top)+0.75rem)] pb-3">
        <h1 className="text-xl font-semibold">Week</h1>
        {t && <p className="text-sm text-muted-foreground">{spanLabel(t)}</p>}
      </header>
      <main className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 px-4 pt-4 pb-[calc(env(safe-area-inset-bottom)+1.5rem)]">
          {!weeks || !t ? (
            error ? (
              <div className="flex flex-col items-start gap-2">
                <p className="text-sm text-destructive">{error}</p>
                {onRetry && (
                  <Button size="sm" variant="outline" onClick={onRetry}>
                    Try again
                  </Button>
                )}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">Loading…</p>
            )
          ) : (
            <>
              <ul className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6">
                {METRICS.map((m) => (
                  <Tile key={m.key} metric={m} weeks={weeks} current={t} />
                ))}
              </ul>
              <div className="grid gap-6 md:grid-cols-2">
                <Section title="How the gate answered">
                  <Bars rows={GATE.map(([key, label]) => [label, t.gate.by_decision[key] ?? 0])} />
                  {Object.values(t.gate.reasons).some((n) => n > 0) && (
                    <ul className="flex flex-col gap-0.5 pt-1 text-xs text-muted-foreground">
                      {Object.entries(t.gate.reasons).filter(([, n]) => n > 0).map(([why, n]) => (
                        <li key={why} className="flex gap-2">
                          <span className="min-w-0 flex-1 truncate" title={why}>
                            {why}
                          </span>
                          <span className="tabular-nums">{n}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </Section>
                <Section title="Cards">
                  <Bars
                    rows={[
                      ["Granted", t.cards.approval.granted],
                      ["Refused", t.cards.approval.rejected],
                      ["Expired or stale", t.cards.approval.lapsed],
                      ["Still open", t.cards.approval.open],
                      ["Questions answered", t.cards.question.answered],
                      ["Owner-only (courier)", t.cards.courier],
                    ]}
                  />
                </Section>
                <Section title="Tool errors">
                  <Bars rows={Object.entries(t.engine.tool_errors_by_tool)} />
                </Section>
                <Section title="Sessions">
                  <Bars
                    rows={[
                      ["Compactions", t.engine.compactions],
                      ["Turns that failed", t.engine.run_errors],
                      ["Stopped by you", t.owner.stops],
                    ]}
                  />
                </Section>
              </div>
            </>
          )}
        </div>
      </main>
    </div>
  );
}

export function WeekPage({ load = api.stats }: { load?: typeof api.stats }) {
  const [weeks, setWeeks] = useState<Tally[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  // biome-ignore lint/correctness/useExhaustiveDependencies: `attempt` is the retry
  useEffect(() => {
    let gone = false;
    setError(null);
    load()
      .then((r) => !gone && setWeeks(r.weeks))
      .catch((e) => !gone && setError(e instanceof ApiError ? e.message : "The numbers did not load."));
    return () => {
      gone = true;
    };
  }, [load, attempt]);
  return <WeekView weeks={weeks} error={error} onRetry={() => setAttempt((n) => n + 1)} />;
}
