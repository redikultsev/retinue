// A `chart` view: the records counted by week, this week last, as columns stacked by the enum's values in its order —
// the first series in the accent, the rest in quiet steps, by place and never by rank — on a quiet grid; the total
// over the peak and over this week, each week's numbers in its tooltip, a legend with the sums for two series or more,
// and every number in a table for a screen reader.
import { useEffect, useState } from "react";
import type { ChartView, CollectionViews } from "@/lib/collections";
import { valueWords } from "@/lib/collections";
import { day } from "@/lib/spaces";

// One accent: the first series, which the form puts first because it matters; the rest in quiet steps of the ink.
const SERIES = ["fill-warning", "fill-muted-foreground/35", "fill-muted-foreground/20", "fill-muted-foreground/55", "fill-muted-foreground/10"];
const SWATCH = ["bg-warning", "bg-muted-foreground/35", "bg-muted-foreground/20", "bg-muted-foreground/55", "bg-muted-foreground/10"];

export function Chart({ collection, view }: { collection: CollectionViews; view: ChartView }) {
  const labels = view.group ? collection.labels?.[view.group] : undefined;
  const series: (string | null)[] = view.group ? [...view.values, null] : [null];
  const count = (w: ChartView["weeks"][number], v: string | null) => w.counts.find((c) => c.value === v)?.count ?? 0;
  const totals = view.weeks.map((w) => w.counts.reduce((n, c) => n + c.count, 0));
  const most = Math.max(1, ...totals);
  const step = most <= 10 ? Math.ceil(most / 2) : Math.ceil(most / 2 / 5) * 5;
  const top = step * 2;
  const used = series.filter((v) => view.weeks.some((w) => count(w, v) > 0));
  const peak = totals.indexOf(Math.max(...totals));
  const [box, setBox] = useState<SVGSVGElement | null>(null);
  const width = useWidth(box) || 640;
  const H = 160, TOP = 18, LEFT = 24;
  const slot = (width - LEFT) / Math.max(1, view.weeks.length);
  const W = Math.min(48, Math.round(slot * 0.55)), GAP = slot - W;
  const y = (n: number) => TOP + H - (n / top) * H;
  const name = (v: string | null) => (v === null ? (view.group ? "Unknown" : view.title) : valueWords(v, labels));
  const sum = (v: string | null) => view.weeks.reduce((n, w) => n + count(w, v), 0);
  return (
    <figure className="flex flex-col gap-3 rounded-xl border bg-card px-4 py-3" data-testid={`chart-${view.name}`}>
      <figcaption className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span className="text-[13px] font-medium">{view.title}</span>
        {used.length > 1 &&
          used.map((v) => (
            <span key={v ?? ""} className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
              <span className={`size-2 rounded-[2px] ${SWATCH[series.indexOf(v) % SWATCH.length]}`} />
              {name(v)}
              <span className="font-mono tabular-nums">{sum(v)}</span>
            </span>
          ))}
        <span className="ml-auto text-xs text-muted-foreground">{`${view.weeks.length} weeks`}</span>
      </figcaption>
      <svg ref={setBox} viewBox={`0 0 ${width} ${H + TOP + 20}`} height={H + TOP + 20} className="w-full" aria-hidden>
        {[0, step, top].map((n) => (
          <g key={n}>
            <line x1={LEFT} x2={width} y1={y(n) + 0.5} y2={y(n) + 0.5} className={n ? "stroke-border [stroke-dasharray:2_3]" : "stroke-border"} />
            <text x={0} y={y(n) + 4} className="fill-muted-foreground font-mono text-[10px]">
              {n}
            </text>
          </g>
        ))}
        {view.weeks.map((w, i) => {
          const x = LEFT + i * (W + GAP) + GAP / 2;
          const last = i === view.weeks.length - 1;
          let at = TOP + H;
          return (
            <g key={w.first}>
              <title>{`Week of ${day(w.first)}: ${used.map((v) => `${name(v)} ${count(w, v)}`).join(", ")}`}</title>
              {series.map((v, k) => {
                const h = (count(w, v) / top) * H;
                if (!h) return null;
                at -= h;
                return <rect key={v ?? ""} x={x} y={at + 1} width={W} height={Math.max(h - 1, 1)} rx={3} className={SERIES[k % SERIES.length]} />;
              })}
              {totals[i]! > 0 && (i === peak || last) && (
                <text x={x + W / 2} y={at - 6} textAnchor="middle" className="fill-foreground font-mono text-[11px]">
                  {totals[i]}
                </text>
              )}
              <text x={x + W / 2} y={TOP + H + 15} textAnchor="middle" className={last ? "fill-foreground font-mono text-[10px]" : "fill-muted-foreground font-mono text-[10px]"}>
                {day(w.first)}
              </text>
            </g>
          );
        })}
      </svg>
      <table className="sr-only">
        <caption>{view.title}</caption>
        <thead>
          <tr>
            <th>Week of</th>
            {used.map((v) => (
              <th key={v ?? ""}>{name(v)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {view.weeks.map((w) => (
            <tr key={w.first}>
              <td>{day(w.first)}</td>
              {used.map((v) => (
                <td key={v ?? ""}>{count(w, v)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}

/** An element's width in pixels, kept as it changes: the chart drawn at its own size, its text never scaled. */
function useWidth(el: Element | null): number {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!el) return;
    setWidth(el.getBoundingClientRect().width);
    if (typeof ResizeObserver === "undefined") return;
    const seen = new ResizeObserver(([entry]) => entry && setWidth(entry.contentRect.width));
    seen.observe(el);
    return () => seen.disconnect();
  }, [el]);
  return Math.round(width);
}
