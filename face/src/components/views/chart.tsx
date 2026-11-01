// A `chart` view: the records counted by week, this week last, as columns stacked by the enum's values in its order —
// the series by the chart colours in that order, never by rank — each total over its column, a legend above for two
// series or more, the weeks under it, and every number in a table for a screen reader.
import type { ChartView, CollectionViews } from "@/lib/collections";
import { valueWords } from "@/lib/collections";
import { day } from "@/lib/spaces";

const SERIES = ["fill-chart-1", "fill-chart-2", "fill-chart-3", "fill-chart-4", "fill-chart-5"];
const SWATCH = ["bg-chart-1", "bg-chart-2", "bg-chart-3", "bg-chart-4", "bg-chart-5"];

export function Chart({ collection, view }: { collection: CollectionViews; view: ChartView }) {
  const labels = view.group ? collection.labels?.[view.group] : undefined;
  const series: (string | null)[] = view.group ? [...view.values, null] : [null];
  const count = (w: ChartView["weeks"][number], v: string | null) => w.counts.find((c) => c.value === v)?.count ?? 0;
  const totals = view.weeks.map((w) => w.counts.reduce((n, c) => n + c.count, 0));
  const top = Math.max(1, ...totals);
  const used = series.filter((v) => view.weeks.some((w) => count(w, v) > 0));
  const W = 40, GAP = 14, H = 110, TOP = 18;
  const width = view.weeks.length * (W + GAP);
  const name = (v: string | null) => (v === null ? (view.group ? "Unknown" : view.title) : valueWords(v, labels));
  return (
    <figure className="flex flex-col gap-2 rounded-lg border bg-card p-3" data-testid={`chart-${view.name}`}>
      {used.length > 1 && (
        <figcaption className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          {used.map((v) => (
            <span key={v ?? ""} className="inline-flex items-center gap-1.5">
              <span className={`size-2.5 rounded-sm ${SWATCH[series.indexOf(v) % SWATCH.length]}`} />
              {name(v)}
            </span>
          ))}
        </figcaption>
      )}
      <svg viewBox={`0 0 ${width} ${H + TOP + 18}`} className="h-auto w-full max-w-xl" aria-hidden>
        {view.weeks.map((w, i) => {
          const x = i * (W + GAP) + GAP / 2;
          let y = TOP + H;
          return (
            <g key={w.first}>
              {series.map((v, k) => {
                const h = (count(w, v) / top) * H;
                if (!h) return null;
                y -= h;
                return <rect key={v ?? ""} x={x} y={y + (k ? 1 : 0)} width={W} height={Math.max(h - (k ? 1 : 0), 1)} rx={3} className={SERIES[k % SERIES.length]} />;
              })}
              {totals[i]! > 0 && (
                <text x={x + W / 2} y={y - 5} textAnchor="middle" className="fill-muted-foreground text-[11px]">
                  {totals[i]}
                </text>
              )}
              <text x={x + W / 2} y={TOP + H + 14} textAnchor="middle" className="fill-muted-foreground text-[11px]">
                {day(w.first)}
              </text>
            </g>
          );
        })}
        <line x1={0} x2={width} y1={TOP + H + 0.5} y2={TOP + H + 0.5} className="stroke-border" />
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
