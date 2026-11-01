// A `board` view: a column per value of its enum, in the enum's order, each card with its days in the
// column — «stuck» past the form's threshold. A wide screen shows the columns side by side, the folded ones as
// closed lists below; a phone shows one column at a time, with a switch of columns above. Fewer than three records
// make no board: they are a list, each with its column's name. Where the screen writes, a card opens its record's
// sheet with «Move to» the other columns — the phone's way, and the keyboard's — and on a wide screen it is dragged
// to another column, the folded ones included; a card moved shows in its new column at once.
import { createContext, useContext, useMemo, useState } from "react";
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
  type Labels,
  type ViewRow,
  valueWords,
} from "@/lib/collections";
import { cn, LIST } from "@/lib/utils";
import { type Editing, useEditing } from "./edit";
import { Dropped, Parts, Row } from "./queue";

/** What the board's cards do where the screen writes: open a record's sheet, and move one to another column. */
interface Moving {
  open: (row: ViewRow) => void;
  /** A card dropped on a column: moved there, unless it is there already. */
  drop: (id: string, value: string | null) => void;
}

const DRAGGED = "application/x-retinue-record";

/** The form's words for the board's values, and for the enums its cards show. */
const Words = createContext<{ labels: Labels; group: string }>({ labels: {}, group: "" });

/** A column's value in the form's words. */
function useColumnWords() {
  const { labels, group } = useContext(Words);
  return (value: string | null) => valueWords(value, labels[group]);
}

function Card({ row, fields, moving }: { row: ViewRow; fields: string[]; moving: Moving | null }) {
  const { title, rest } = rowWords(row, fields, undefined, useContext(Words).labels);
  const { text, late } = daysWords(row, "board");
  const words = (
    <>
      <span dir="auto" className="text-sm leading-snug break-words">
        {title}
      </span>
      {rest.length > 0 && (
        <span className="text-xs break-words text-muted-foreground">
          <Parts parts={rest} />
        </span>
      )}
      {(text || late) && (
        <span className={cn("text-xs tabular-nums", late ? "font-medium text-destructive" : "text-muted-foreground")}>
          {late ? `${late} · ${text}` : `${text} here`}
        </span>
      )}
    </>
  );
  const look = cn("flex flex-col gap-0.5 rounded-lg border bg-card px-2.5 py-2", row.rotten && "border-destructive/60");
  if (!moving) return <li className={look}>{words}</li>;
  return (
    <li
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData(DRAGGED, row.id);
        e.dataTransfer.effectAllowed = "move";
      }}
      className="cursor-grab active:cursor-grabbing"
    >
      <button type="button" onClick={() => moving.open(row)} className={cn(look, "w-full text-left transition-colors hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none")}>
        {words}
      </button>
    </li>
  );
}

/** A column's props for taking a dragged card; none where the screen does not write, or for the unknown column. */
function useDrop(moving: Moving | null, value: string | null) {
  const [over, setOver] = useState(false);
  if (!moving || value === null) return { over: false, props: {} };
  const props = {
    onDragOver: (e: React.DragEvent) => {
      if (!e.dataTransfer.types.includes(DRAGGED)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      setOver(true);
    },
    onDragLeave: () => setOver(false),
    onDrop: (e: React.DragEvent) => {
      e.preventDefault();
      setOver(false);
      const id = e.dataTransfer.getData(DRAGGED);
      if (id) moving.drop(id, value);
    },
  };
  return { over, props };
}

const heading = (c: Column, words: (v: string | null) => string) => `${words(c.value)} (${c.count})`;

function Rows({ column, fields, moving }: { column: Column; fields: string[]; moving: Moving | null }) {
  const { labels } = useContext(Words);
  return column.rows.length ? (
    <ul className={LIST}>
      {column.rows.map((row) => (
        <li key={row.id}>
          <Row row={row} fields={fields} kind="board" labels={labels} onOpen={moving ? () => moving.open(row) : undefined} />
        </li>
      ))}
    </ul>
  ) : (
    <p className="px-1 py-2 text-sm text-muted-foreground">{column.count ? "Not shown: the answer would be too large." : "None."}</p>
  );
}

/** The phone's board: one column at a time. */
function Switched({ view, moving }: { view: BoardView; moving: Moving | null }) {
  const [at, setAt] = useState(() => openColumn(view));
  const words = useColumnWords();
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
              "flex min-h-11 shrink-0 items-center gap-1 rounded-full border px-3 text-sm whitespace-nowrap",
              i === at ? "border-primary bg-primary text-primary-foreground" : c.folded || !c.count ? "text-muted-foreground" : "bg-card",
              c.rows.some((r) => r.rotten) && i !== at && "border-destructive/60",
            )}
          >
            {words(c.value)}
            <span className="tabular-nums opacity-80">{c.count}</span>
          </button>
        ))}
      </fieldset>
      {column && <Rows column={column} fields={view.fields} moving={moving} />}
    </div>
  );
}

/** The wide screen's board: the open columns side by side, narrowing to fit (five fit the page's column), scrolled
 * within the board only past that; the folded ones below, closed. */
function OpenColumn({ column, fields, moving }: { column: Column; fields: string[]; moving: Moving | null }) {
  const { over, props } = useDrop(moving, column.value);
  const words = useColumnWords();
  return (
    <section
      aria-label={heading(column, words)}
      data-testid={`column-${column.value}`}
      className={cn("flex min-w-32 flex-1 basis-0 flex-col gap-1.5 rounded-xl bg-muted/60 p-2 transition-shadow", over && "ring-2 ring-primary")}
      {...props}
    >
      <h3 className={cn("px-0.5 text-xs font-semibold", !column.count && "text-muted-foreground")}>{heading(column, words)}</h3>
      {column.rows.length > 0 && (
        <ul className="flex flex-col gap-1.5">
          {column.rows.map((row) => (
            <Card key={row.id} row={row} fields={fields} moving={moving} />
          ))}
        </ul>
      )}
    </section>
  );
}

function FoldedColumn({ column, fields, moving }: { column: Column; fields: string[]; moving: Moving | null }) {
  const { over, props } = useDrop(moving, column.value);
  const words = useColumnWords();
  return (
    <details data-testid={`column-${column.value}`} className={cn("rounded-lg border px-3 py-2", over && "ring-2 ring-primary")} {...props}>
      <summary className="cursor-pointer text-sm font-medium">{heading(column, words)}</summary>
      <div className="pt-2">
        <Rows column={column} fields={fields} moving={moving} />
      </div>
    </details>
  );
}

function Columns({ view, moving }: { view: BoardView; moving: Moving | null }) {
  const open = view.columns.filter((c) => !c.folded);
  const folded = view.columns.filter((c) => c.folded);
  return (
    <div className="hidden flex-col gap-2 sm:flex" data-testid={`board-wide-${view.name}`}>
      <div className="flex gap-2 overflow-x-auto pb-1">
        {open.map((c) => (
          <OpenColumn key={String(c.value)} column={c} fields={view.fields} moving={moving} />
        ))}
      </div>
      {folded.map((c) => (
        <FoldedColumn key={String(c.value)} column={c} fields={view.fields} moving={moving} />
      ))}
      {moving && <p className="px-1 text-xs text-muted-foreground">Drag a card to another column, or open it to move it.</p>}
    </div>
  );
}

/** The board with the cards moved but not yet read again in their new columns, each at the top of it. */
export function movedBoard(view: BoardView, moved: Record<string, string>): BoardView {
  if (!Object.keys(moved).length) return view;
  const cards = new Map(view.columns.flatMap((c) => c.rows.map((r) => [r.id, r] as const)));
  return {
    ...view,
    columns: view.columns.map((c) => {
      const kept = c.rows.filter((r) => !(r.id in moved) || moved[r.id] === c.value);
      const come = Object.entries(moved)
        .filter(([id, to]) => to === c.value && !c.rows.some((r) => r.id === id))
        .flatMap(([id]) => (cards.get(id) ? [{ ...cards.get(id)!, days: 0, rotten: false }] : []));
      return { ...c, rows: [...come, ...kept], count: c.count - (c.rows.length - kept.length) + come.length };
    }),
  };
}

function useMoving(collection: CollectionViews, view: BoardView, editing: Editing | null) {
  const [moved, setMoved] = useState<Record<string, string>>({});
  const [seen, setSeen] = useState(view);
  if (seen !== view) {
    setSeen(view); // read again: every card is where it is now
    setMoved({});
  }
  const shown = useMemo(() => movedBoard(view, moved), [view, moved]);
  const moving = useMemo<Moving | null>(() => {
    if (!editing) return null;
    const columns = view.columns.map((c) => c.value);
    return {
      open: (row) => editing.open(collection.name, row.id, { field: view.group, values: columns }),
      drop: (id, value) => {
        const card = view.columns.flatMap((c) => c.rows.map((r) => ({ r, at: c.value }))).find((x) => x.r.id === id);
        if (!card || value === null || (moved[id] ?? card.at) === value) return;
        setMoved((m) => ({ ...m, [id]: value }));
        const step = { op: { op: "set" as const, id, version: card.r.version, fields: { [view.group]: value } }, target: id };
        const said = `${rowWords(card.r, view.fields, undefined, collection.labels).title}: moved to ${valueWords(value, collection.labels?.[view.group])}.`;
        void editing.write(collection.name, [step], said).then((written) => {
          if (!written)
            setMoved((m) => {
              const { [id]: _, ...rest } = m;
              return rest;
            });
        });
      },
    };
  }, [collection.name, collection.labels, editing, moved, view]);
  return { shown, moving };
}

/** Too few for a board: one list, each row with its column's name. */
function Few({ view, moving }: { view: BoardView; moving: Moving | null }) {
  const words = useColumnWords();
  const { labels } = useContext(Words);
  return (
    <ul className={LIST} data-testid={`board-list-${view.name}`}>
      {view.columns.flatMap((c) =>
        c.rows.map((row) => (
          <li key={row.id}>
            <Row row={row} fields={view.fields} kind="board" labels={labels} label={words(c.value)} onOpen={moving ? () => moving.open(row) : undefined} />
          </li>
        )),
      )}
    </ul>
  );
}

export function Board({ collection, view: given }: { collection: CollectionViews; view: BoardView }) {
  const { shown: view, moving } = useMoving(collection, given, useEditing());
  const total = boardTotal(view);
  const words = useMemo(() => ({ labels: collection.labels ?? {}, group: view.group }), [collection.labels, view.group]);
  if (total === 0) return <p className="px-1 py-2 text-sm text-muted-foreground">{emptyWords(collection)}</p>;
  return (
    <Words.Provider value={words}>
    <div className="flex flex-col gap-1">
      {total < BOARD_MIN ? (
        <Few view={view} moving={moving} />
      ) : (
        <>
          <Switched view={view} moving={moving} />
          <Columns view={view} moving={moving} />
        </>
      )}
      <Dropped n={view.dropped} />
    </div>
    </Words.Provider>
  );
}
