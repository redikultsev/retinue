// The figures beside a chart: sums by month as columns, this month last, stacked by the enum's values in its order (the
// first in the accent, the rest in quiet steps); a year's line, the running total through this year over last year's in
// a quiet line; and a project's own view in code, its page in a sandboxed frame that gets its data from here by message.
// Each says its numbers in words and in a table for a screen reader.
import { useCallback, useEffect, useRef, useState } from "react";
import type { CodeView, CollectionViews, MonthsView, Sum, View, YearView } from "@/lib/collections";
import { valueWords } from "@/lib/collections";
import { useWidth } from "./chart";

const SERIES = ["fill-warning", "fill-muted-foreground/35", "fill-muted-foreground/20", "fill-muted-foreground/55", "fill-muted-foreground/10"];
const MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export type Figured = MonthsView | YearView | CodeView;

/** Whether a view is drawn here rather than by the chart. */
export const isFigure = (view: View): view is Figured => view.error === undefined && (view.kind === "months" || view.kind === "year" || view.kind === "code");

/** A sum in words: money in its currency («€1,250.50»), a count or a number with its thousands marked. */
export function sumWords(total: Sum, unit: string | null): string {
  const n = Number(total);
  if (!unit) return n.toLocaleString("en-US");
  try {
    return n.toLocaleString("en-US", { style: "currency", currency: unit });
  } catch {
    return `${n.toFixed(2)} ${unit}`;
  }
}

/** A month's first day as «Sep», with its year when it is not this one's. */
const monthWords = (first: string, year: number) => `${MONTH[Number(first.slice(5, 7)) - 1] ?? first}${Number(first.slice(0, 4)) === year ? "" : ` ${first.slice(0, 4)}`}`;

/** The top of a scale over `most`, and its middle step: round numbers, never below one. */
function scale(most: number): { step: number; top: number } {
  const half = Math.max(1, most) / 2;
  const power = 10 ** Math.floor(Math.log10(half));
  const step = Math.ceil(half / power) * power;
  return { step, top: step * 2 };
}

export function Months({ collection, view }: { collection: CollectionViews; view: MonthsView }) {
  const labels = view.group ? collection.labels?.[view.group] : undefined;
  const series: (string | null)[] = view.group ? [...view.values, null] : [null];
  const amount = (m: MonthsView["months"][number], v: string | null) => Number(m.totals.find((t) => t.value === v)?.total ?? 0);
  const totals = view.months.map((m) => m.totals.reduce((n, t) => n + Number(t.total), 0));
  const used = series.filter((v) => view.months.some((m) => amount(m, v) > 0));
  const { step, top } = scale(Math.max(...totals, 0));
  const year = Number(view.months.at(-1)?.first.slice(0, 4) ?? 0);
  const [box, setBox] = useState<SVGSVGElement | null>(null);
  const width = useWidth(box) || 640;
  const H = 160, TOP = 18, LEFT = 48;
  const slot = (width - LEFT) / Math.max(1, view.months.length);
  const W = Math.min(40, Math.round(slot * 0.6));
  const y = (n: number) => TOP + H - (n / top) * H;
  const name = (v: string | null) => (v === null ? (view.group ? "Unknown" : view.title) : valueWords(v, labels));
  const now = totals.at(-1) ?? 0;
  return (
    <figure className="flex flex-col gap-3 rounded-xl border bg-card px-4 py-3" data-testid={`months-${view.name}`}>
      <figcaption className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span className="text-[13px] font-medium">{view.title}</span>
        <span className="text-xs text-muted-foreground">
          This month <span className="font-mono text-foreground tabular-nums">{sumWords(now, view.unit)}</span>
        </span>
        <span className="ml-auto text-xs text-muted-foreground">{`${view.months.length} months`}</span>
      </figcaption>
      <svg ref={setBox} viewBox={`0 0 ${width} ${H + TOP + 20}`} height={H + TOP + 20} className="w-full" aria-hidden>
        {[0, step, top].map((n) => (
          <g key={n}>
            <line x1={LEFT} x2={width} y1={y(n) + 0.5} y2={y(n) + 0.5} className={n ? "stroke-border [stroke-dasharray:2_3]" : "stroke-border"} />
            <text x={0} y={y(n) + 4} className="fill-muted-foreground font-mono text-[11px]">
              {sumWords(n, null)}
            </text>
          </g>
        ))}
        {view.months.map((m, i) => {
          const x = LEFT + i * slot + (slot - W) / 2;
          let at = TOP + H;
          return (
            <g key={m.first}>
              <title>{`${monthWords(m.first, year)}: ${sumWords(totals[i]!, view.unit)}`}</title>
              {series.map((v, k) => {
                const h = (amount(m, v) / top) * H;
                if (h <= 0) return null;
                at -= h;
                return <rect key={v ?? ""} x={x} y={at + 1} width={W} height={Math.max(h - 1, 1)} rx={3} className={SERIES[k % SERIES.length]} />;
              })}
              <text x={x + W / 2} y={TOP + H + 15} textAnchor="middle" className={i === view.months.length - 1 ? "fill-foreground font-mono text-[11px]" : "fill-muted-foreground font-mono text-[11px]"}>
                {monthWords(m.first, year)}
              </text>
            </g>
          );
        })}
      </svg>
      <table className="sr-only">
        <caption>{view.title}</caption>
        <thead>
          <tr>
            <th>Month</th>
            {used.map((v) => (
              <th key={v ?? ""}>{name(v)}</th>
            ))}
            <th>Total</th>
          </tr>
        </thead>
        <tbody>
          {view.months.map((m, i) => (
            <tr key={m.first}>
              <td>{monthWords(m.first, year)}</td>
              {used.map((v) => (
                <td key={v ?? ""}>{sumWords(amount(m, v), view.unit)}</td>
              ))}
              <td>{sumWords(totals[i]!, view.unit)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}

export function YearLine({ view }: { view: YearView }) {
  const [box, setBox] = useState<SVGSVGElement | null>(null);
  const width = useWidth(box) || 640;
  const H = 140, TOP = 12, LEFT = 48;
  const run = (m: { running: Sum }) => Number(m.running);
  const shown = view.months.slice(0, view.through);
  const { step, top } = scale(Math.max(0, ...shown.map(run), ...view.previous.map(run)));
  const x = (i: number) => LEFT + (i / 11) * (width - LEFT - 8);
  const y = (n: number) => TOP + H - (n / top) * H;
  const points = (months: { running: Sum }[]) => months.map((m, i) => `${x(i)},${y(run(m))}`).join(" ");
  const now = shown.at(-1);
  const then = view.previous[view.through - 1];
  return (
    <figure className="flex flex-col gap-3 rounded-xl border bg-card px-4 py-3" data-testid={`year-${view.name}`}>
      <figcaption className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span className="text-[13px] font-medium">{view.title}</span>
        <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <span className="size-2 rounded-[2px] bg-warning" />
          {view.year} <span className="font-mono text-foreground tabular-nums">{now ? sumWords(now.running, view.unit) : "—"}</span>
        </span>
        <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <span className="size-2 rounded-[2px] bg-muted-foreground/35" />
          {`${view.year - 1} by then`} <span className="font-mono tabular-nums">{then ? sumWords(then.running, view.unit) : "—"}</span>
        </span>
      </figcaption>
      <svg ref={setBox} viewBox={`0 0 ${width} ${H + TOP + 20}`} height={H + TOP + 20} className="w-full" aria-hidden>
        {[0, step, top].map((n) => (
          <g key={n}>
            <line x1={LEFT} x2={width} y1={y(n) + 0.5} y2={y(n) + 0.5} className={n ? "stroke-border [stroke-dasharray:2_3]" : "stroke-border"} />
            <text x={0} y={y(n) + 4} className="fill-muted-foreground font-mono text-[11px]">
              {sumWords(n, null)}
            </text>
          </g>
        ))}
        <polyline points={points(view.previous)} fill="none" strokeWidth={1.5} className="stroke-muted-foreground/35" data-line="previous" />
        <polyline points={points(shown)} fill="none" strokeWidth={2} className="stroke-warning" data-line="this" />
        {[0, 3, 6, 9, 11].map((i) => (
          <text key={i} x={x(i)} y={TOP + H + 15} textAnchor="middle" className="fill-muted-foreground font-mono text-[11px]">
            {MONTH[i]}
          </text>
        ))}
      </svg>
      <table className="sr-only">
        <caption>{view.title}</caption>
        <thead>
          <tr>
            <th>Month</th>
            <th>{view.year}</th>
            <th>{view.year - 1}</th>
          </tr>
        </thead>
        <tbody>
          {view.previous.map((m, i) => (
            <tr key={m.first}>
              <td>{MONTH[i]}</td>
              <td>{shown[i] ? sumWords(shown[i]!.running, view.unit) : ""}</td>
              <td>{sumWords(m.running, view.unit)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}

/** A project's own view: its page in a frame under the platform's sandbox — no network, no origin of the app's — and
 * its data from here, posted to the frame whenever it loads or the data changes. Nothing the frame says is read. */
export function CodeFrame({ view }: { view: CodeView }) {
  const frame = useRef<HTMLIFrameElement>(null);
  const send = useCallback(() => {
    const { name, title, fields, labels, count, rows, dropped } = view;
    frame.current?.contentWindow?.postMessage({ view: { name, title, fields, labels, count, rows, dropped } }, "*");
  }, [view]);
  useEffect(send, [send]);
  return (
    <figure className="flex flex-col gap-3 rounded-xl border bg-card px-4 py-3" data-testid={`code-${view.name}`}>
      <figcaption className="text-[13px] font-medium">{view.title}</figcaption>
      <iframe ref={frame} title={view.title} src={view.src} sandbox={view.sandbox} referrerPolicy="no-referrer" onLoad={send} className="h-80 w-full rounded border bg-white" />
    </figure>
  );
}

export function Figure({ collection, view }: { collection: CollectionViews; view: Figured }) {
  if (view.kind === "months") return <Months collection={collection} view={view} />;
  if (view.kind === "year") return <YearLine view={view} />;
  return <CodeFrame view={view} />;
}
