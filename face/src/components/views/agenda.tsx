// The days ahead: every `agenda` view of a tab on one strip of days, today first and shaded, a day with records on it
// marked; under the strip those records, the soonest first, each its day and its words. A record opens its sheet.
import type { AgendaView, Placed, ViewRow } from "@/lib/collections";
import { lineWords } from "@/lib/collections";
import { cn } from "@/lib/utils";
import { useEditing } from "./edit";

const WEEKDAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** The days from `first` to `last`, as ISO days. */
export function daysOf(first: string, last: string): string[] {
  const out: string[] = [];
  for (let d = new Date(`${first}T00:00:00Z`); d.toISOString().slice(0, 10) <= last; d = new Date(d.getTime() + 86_400_000))
    out.push(d.toISOString().slice(0, 10));
  return out;
}

export function Agenda({ agendas }: { agendas: Placed[] }) {
  const editing = useEditing();
  const shown = agendas.filter((p): p is Placed<AgendaView> => p.view.error === undefined && p.view.kind === "agenda");
  if (!shown.length) return null;
  const first = shown.map((p) => p.view.first).sort()[0]!;
  const last = shown.map((p) => p.view.last).sort().at(-1)!;
  const on = new Map<string, { placed: Placed<AgendaView>; row: ViewRow }[]>();
  for (const placed of shown)
    for (const row of placed.view.rows) {
      const date = String(row.fields[placed.view.date] ?? "");
      on.set(date, [...(on.get(date) ?? []), { placed, row }]);
    }
  const days = daysOf(first, last);
  const name = (date: string) => {
    const d = new Date(`${date}T00:00:00Z`);
    return { weekday: WEEKDAY[d.getUTCDay()]!, date: d.getUTCDate() };
  };
  const events = days.flatMap((date) => (on.get(date) ?? []).map((e) => ({ date, ...e })));
  return (
    <div className="overflow-hidden rounded-lg border bg-card" data-testid="agenda">
      <ol className="grid grid-cols-7 gap-0.5 p-1.5">
        {days.map((date, i) => {
          const { weekday, date: n } = name(date);
          const busy = on.has(date);
          return (
            <li
              key={date}
              aria-label={`${weekday} ${n}${busy ? `: ${on.get(date)!.length}` : ""}`}
              className={cn("flex flex-col items-center gap-0.5 rounded-md py-1", i === 0 && "bg-accent")}
            >
              <span aria-hidden className={cn("text-[11px]", i === 0 ? "text-foreground" : "text-muted-foreground")}>
                {weekday}
              </span>
              <span
                aria-hidden
                className={cn(
                  "min-w-6 rounded px-1 text-center font-mono text-[13px] tabular-nums",
                  busy ? "bg-warning font-medium text-warning-foreground" : i === 0 ? "text-foreground" : "text-muted-foreground",
                )}
              >
                {n}
              </span>
            </li>
          );
        })}
      </ol>
      {events.length === 0 && <p className="border-t px-3 py-2 text-[13px] text-muted-foreground">Nothing on these days.</p>}
      {events.length > 0 && (
        <ul className="flex flex-col divide-y border-t">
          {events.map(({ date, placed, row }) => {
            const { title, beside } = lineWords(row, placed.view.fields.filter((f) => f !== placed.view.date), placed.collection.labels);
            const { weekday, date: n } = name(date);
            const words = (
              <>
                <span className="font-mono text-xs text-muted-foreground tabular-nums">{`${weekday} ${n}`}</span>
                <span className="min-w-0">
                  <span className="font-medium">{title}</span>
                  {beside && <span className="text-muted-foreground">{` ${beside}`}</span>}
                </span>
              </>
            );
            const line = "grid w-full grid-cols-[3.5rem_minmax(0,1fr)] items-baseline gap-2 px-3 py-2 text-left text-[13px]";
            return (
              <li key={`${date}/${row.id}`}>
                {editing ? (
                  <button type="button" onClick={() => editing.open(placed.collection.name, row.id)} className={cn(line, "hover:bg-accent/50")}>
                    {words}
                  </button>
                ) : (
                  <span className={line}>{words}</span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
