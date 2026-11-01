// A board on a Space's first tab, as a strip: a bar split by its columns, each part as wide as its count, and under it
// the columns in the enum's order — each its count and name, the empty ones said once together, the folded ones at
// the end. A column pressed opens its rows below the strip, each opening its record's sheet, where its column moves
// it; the full board, dragged by hand, is on the board's own tab when its view names one.
import { useState } from "react";
import type { BoardView, CollectionViews, Column } from "@/lib/collections";
import { valueWords } from "@/lib/collections";
import { cn, LIST } from "@/lib/utils";
import { useEditing } from "./edit";
import { Dropped, Row } from "./queue";

export function Strip({ collection, view }: { collection: CollectionViews; view: BoardView }) {
  const [open, setOpen] = useState<string | null>(null);
  const editing = useEditing();
  const labels = collection.labels?.[view.group];
  const key = (c: Column) => c.value ?? "";
  const live = view.columns.filter((c) => !c.folded);
  const folded = view.columns.filter((c) => c.folded);
  const empty = live.filter((c) => c.count === 0);
  const shown = view.columns.find((c) => key(c) === open) ?? null;
  const values = view.columns.map((c) => c.value);
  const name = (c: Column) => valueWords(c.value, labels);
  const label = (c: Column) => (
    <button
      key={key(c)}
      type="button"
      aria-expanded={open === key(c)}
      onClick={() => setOpen(open === key(c) ? null : key(c))}
      className={cn(
        "inline-flex min-h-8 items-baseline gap-1.5 rounded-md px-1.5 text-[13px] whitespace-nowrap text-muted-foreground hover:bg-accent hover:text-foreground",
        open === key(c) && "bg-accent text-foreground",
      )}
    >
      <span className="font-mono text-xs font-medium text-foreground tabular-nums">{c.count}</span>
      {name(c)}
    </button>
  );
  return (
    <div className="flex flex-col gap-1" data-testid={`strip-${view.name}`}>
      <div aria-hidden className="flex h-2 gap-0.5">
        {[...live, ...folded]
          .filter((c) => c.count > 0)
          .map((c) => (
            <span
              key={key(c)}
              style={{ flexGrow: c.count }}
              className={cn(
                "min-w-1 basis-0 rounded-[2px] transition-colors",
                open === key(c) ? "bg-foreground" : c.folded ? "bg-muted-foreground/20" : "bg-muted-foreground/45",
              )}
            />
          ))}
      </div>
      <div className="-mx-1.5 flex flex-wrap items-center gap-x-1">
        {live.filter((c) => c.count > 0).map(label)}
        {empty.length > 0 && (
          <span className="px-1.5 text-[13px] whitespace-nowrap text-muted-foreground/80">
            {empty.map(name).join(", ")} <span className="font-mono text-xs tabular-nums">0</span>
          </span>
        )}
        <span className="min-w-3 flex-1" />
        {folded.map(label)}
      </div>
      {shown && (
        <ul className={cn(LIST, "@container")} aria-label={name(shown)}>
          {shown.rows.length === 0 && <li className="px-3 py-2 text-sm text-muted-foreground">Nothing here.</li>}
          {shown.rows.map((row) => (
            <li key={row.id}>
              <Row
                row={row}
                fields={view.fields}
                kind="board"
                labels={collection.labels}
                onOpen={editing ? () => editing.open(collection.name, row.id, { field: view.group, values }) : undefined}
              />
            </li>
          ))}
        </ul>
      )}
      <Dropped n={view.dropped} />
    </div>
  );
}
