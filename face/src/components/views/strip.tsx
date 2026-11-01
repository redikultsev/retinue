// A board on a Space's first tab, as a strip: its columns in the enum's order, each with its count — the empty ones
// dim, the folded ones at the end. A column pressed opens its rows below the strip, each opening its record's sheet,
// where «Move to» moves it; the full board, dragged by hand, is on the board's own tab when its view names one.
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
  const shown = view.columns.find((c) => key(c) === open) ?? null;
  const values = view.columns.map((c) => c.value);
  const chip = (c: Column) => (
    <button
      key={key(c)}
      type="button"
      aria-expanded={open === key(c)}
      onClick={() => setOpen(open === key(c) ? null : key(c))}
      className={cn(
        "inline-flex min-h-9 items-center gap-1 rounded-md px-2 text-[13px] whitespace-nowrap hover:bg-accent md:min-h-8",
        open === key(c) && "bg-accent",
        c.count === 0 && "text-muted-foreground/70",
      )}
    >
      {valueWords(c.value, labels)}
      <span className={cn("font-semibold tabular-nums", c.count === 0 ? "font-normal" : "text-foreground")}>{c.count}</span>
      {c.rows.some((r) => r.rotten) && (
        <>
          <span aria-hidden className="size-1.5 rounded-full bg-destructive" />
          <span className="sr-only">, something stuck</span>
        </>
      )}
    </button>
  );
  return (
    <div className="flex flex-col gap-1" data-testid={`strip-${view.name}`}>
      <div className="-mx-2 flex items-center overflow-x-auto">
        {live.map((c, i) => (
          <span key={key(c)} className="flex items-center">
            {i > 0 && (
              <span aria-hidden className="px-0.5 text-muted-foreground/40">
                ›
              </span>
            )}
            {chip(c)}
          </span>
        ))}
        <span className="min-w-3 flex-1" />
        {folded.map(chip)}
      </div>
      {shown && (
        <ul className={LIST} aria-label={valueWords(shown.value, labels)}>
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
