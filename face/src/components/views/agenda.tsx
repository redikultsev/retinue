// The days ahead: every `agenda` view of a tab on one list of days, today first, each day with the records whose date
// falls on it — a day with none a dash, so the week reads at a glance. A record opens its sheet.
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
  return (
    <ol className="flex flex-col divide-y overflow-hidden rounded-lg border bg-card" data-testid="agenda">
      {daysOf(first, last).map((date, i) => {
        const d = new Date(`${date}T00:00:00Z`);
        return (
          <li key={date} className="grid grid-cols-[3.25rem_minmax(0,1fr)] gap-2 px-3 py-1.5 text-[13px]">
            <span className={cn("tabular-nums", i === 0 ? "font-medium text-primary" : "text-muted-foreground")}>
              {`${WEEKDAY[d.getUTCDay()]} ${d.getUTCDate()}`}
            </span>
            <span className="flex min-w-0 flex-col">
              {(on.get(date) ?? []).map(({ placed, row }) => {
                const { title, beside } = lineWords(row, placed.view.fields.filter((f) => f !== placed.view.date), placed.collection.labels);
                const words = (
                  <>
                    <span className="font-medium">{title}</span>
                    {beside && <span className="text-muted-foreground">{` ${beside}`}</span>}
                  </>
                );
                return editing ? (
                  <button
                    key={row.id}
                    type="button"
                    onClick={() => editing.open(placed.collection.name, row.id)}
                    className="truncate border-l-2 border-warning pl-2 text-left hover:bg-accent/50"
                  >
                    {words}
                  </button>
                ) : (
                  <span key={row.id} className="truncate border-l-2 border-warning pl-2">
                    {words}
                  </span>
                );
              })}
              {!on.get(date) && <span className="text-muted-foreground/50">—</span>}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
