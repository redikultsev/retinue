// A `board` view, read only: a column per value of its enum, in the enum's order, each card with its days in the
// column — «stuck» past the form's threshold. A wide screen shows the columns side by side, the folded ones as
// closed lists below; a phone shows one column at a time, with a switch of columns above. Fewer than three records
// make no board: they are a list, each with its column's name.
import { useState } from "react";
import {
  BOARD_MIN,
  type BoardView,
  boardTotal,
  type CollectionViews,
  type Column,
  daysWords,
  emptyWords,
  openColumn,
  rowWords,
  type ViewRow,
  valueWords,
} from "@/lib/collections";
import { cn, LIST } from "@/lib/utils";
import { Dropped, Row } from "./queue";

function Card({ row, fields }: { row: ViewRow; fields: string[] }) {
  const { title, rest } = rowWords(row, fields);
  const { text, late } = daysWords(row, "board");
  return (
    <li className={cn("flex flex-col gap-0.5 rounded-lg border bg-card px-2.5 py-2", row.rotten && "border-destructive/60")}>
      <span className="text-sm leading-snug break-words">{title}</span>
      {rest.length > 0 && <span className="text-xs break-words text-muted-foreground">{rest.join(" · ")}</span>}
      {(text || late) && (
        <span className={cn("text-xs tabular-nums", late ? "font-medium text-destructive" : "text-muted-foreground")}>
          {late ? `${late} · ${text}` : `${text} here`}
        </span>
      )}
    </li>
  );
}

const heading = (c: Column) => `${valueWords(c.value)} (${c.count})`;

function Rows({ column, fields }: { column: Column; fields: string[] }) {
  return column.rows.length ? (
    <ul className={LIST}>
      {column.rows.map((row) => (
        <li key={row.id}>
          <Row row={row} fields={fields} kind="board" />
        </li>
      ))}
    </ul>
  ) : (
    <p className="px-1 py-2 text-sm text-muted-foreground">{column.count ? "Not shown: the answer would be too large." : "None."}</p>
  );
}

/** The phone's board: one column at a time. */
function Switched({ view }: { view: BoardView }) {
  const [at, setAt] = useState(() => openColumn(view));
  const column = view.columns[at] ?? view.columns[0];
  return (
    <div className="flex flex-col gap-2 sm:hidden" data-testid={`board-phone-${view.name}`}>
      <fieldset aria-label={`${view.title}: column`} className="-mx-1 flex min-w-0 gap-1 overflow-x-auto px-1 pb-1 [scrollbar-width:none]">
        {view.columns.map((c, i) => (
          <button
            key={String(c.value)}
            type="button"
            aria-pressed={i === at}
            onClick={() => setAt(i)}
            className={cn(
              "flex min-h-9 shrink-0 items-center gap-1 rounded-full border px-3 text-sm whitespace-nowrap",
              i === at ? "border-primary bg-primary text-primary-foreground" : c.folded || !c.count ? "text-muted-foreground" : "bg-card",
              c.rows.some((r) => r.rotten) && i !== at && "border-destructive/60",
            )}
          >
            {valueWords(c.value)}
            <span className="tabular-nums opacity-80">{c.count}</span>
          </button>
        ))}
      </fieldset>
      {column && <Rows column={column} fields={view.fields} />}
    </div>
  );
}

/** The wide screen's board: the open columns side by side, scrolled within the board when they do not fit; the folded
 * ones below, closed. */
function Columns({ view }: { view: BoardView }) {
  const open = view.columns.filter((c) => !c.folded);
  const folded = view.columns.filter((c) => c.folded);
  return (
    <div className="hidden flex-col gap-2 sm:flex" data-testid={`board-wide-${view.name}`}>
      <div className="flex gap-2 overflow-x-auto pb-1">
        {open.map((c) => (
          <section key={String(c.value)} aria-label={heading(c)} className="flex w-48 min-w-44 flex-1 shrink-0 flex-col gap-1.5 rounded-xl bg-muted/60 p-2">
            <h3 className={cn("px-0.5 text-xs font-semibold", !c.count && "text-muted-foreground")}>{heading(c)}</h3>
            {c.rows.length > 0 && (
              <ul className="flex flex-col gap-1.5">
                {c.rows.map((row) => (
                  <Card key={row.id} row={row} fields={view.fields} />
                ))}
              </ul>
            )}
          </section>
        ))}
      </div>
      {folded.map((c) => (
        <details key={String(c.value)} className="rounded-lg border px-3 py-2">
          <summary className="cursor-pointer text-sm font-medium">{heading(c)}</summary>
          <div className="pt-2">
            <Rows column={c} fields={view.fields} />
          </div>
        </details>
      ))}
    </div>
  );
}

/** Too few for a board: one list, each row with its column's name. */
function Few({ view }: { view: BoardView }) {
  return (
    <ul className={LIST} data-testid={`board-list-${view.name}`}>
      {view.columns.flatMap((c) =>
        c.rows.map((row) => (
          <li key={row.id}>
            <Row row={row} fields={view.fields} kind="board" label={valueWords(c.value)} />
          </li>
        )),
      )}
    </ul>
  );
}

export function Board({ collection, view }: { collection: CollectionViews; view: BoardView }) {
  const total = boardTotal(view);
  if (total === 0) return <p className="px-1 py-2 text-sm text-muted-foreground">{emptyWords(collection)}</p>;
  return (
    <div className="flex flex-col gap-1">
      {total < BOARD_MIN ? (
        <Few view={view} />
      ) : (
        <>
          <Switched view={view} />
          <Columns view={view} />
        </>
      )}
      <Dropped n={view.dropped} />
    </div>
  );
}
