// A `queue` view: the records a filter keeps, the longest silent first, each with its days of silence — and past the
// form's threshold «overdue», in a word as well as in red. A phone shows the first three and «Show all». Where the
// screen writes, a row opens its record's sheet, and a queue with `done` has its button on each row — «Sent» — and
// «Mark all N» below, asked once; a row pressed leaves at once.
import { Fragment, useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { type CollectionViews, daysWords, emptyWords, type QueueView, rowWords, type ViewRow } from "@/lib/collections";
import type { Step } from "@/lib/edit";
import { cn, LIST, LIST_ROW } from "@/lib/utils";
import { type Editing, useEditing } from "./edit";

/** On a phone: the rows shown before «Show all». */
export const PHONE_ROWS = 3;

/** Words side by side, each isolated: one written right to left does not reorder its neighbours. */
export function Parts({ parts }: { parts: string[] }) {
  return parts.map((part, i) => (
    <Fragment key={i}>
      {i > 0 && " · "}
      <bdi>{part}</bdi>
    </Fragment>
  ));
}

export function Row({
  row,
  fields,
  kind = "queue",
  label,
  onOpen,
  children,
}: {
  row: ViewRow;
  fields: string[];
  kind?: "board" | "queue";
  label?: string;
  /** Opens the record's sheet: the row is a button then. */
  onOpen?: () => void;
  /** A button of its own beside the row: «Sent». */
  children?: React.ReactNode;
}) {
  const { title, rest } = rowWords(row, fields);
  const { text, late } = daysWords(row, kind);
  const body = (
    <>
      <span className="min-w-0 flex-1">
        <span dir="auto" className="block text-[15px] leading-snug break-words md:text-sm">
          {title}
        </span>
        {(rest.length > 0 || label) && (
          <span className="block truncate text-xs text-muted-foreground">
            <Parts parts={[label, ...rest].filter((p): p is string => !!p)} />
          </span>
        )}
      </span>
      {(text || late) && (
        <span className={cn("shrink-0 pt-0.5 text-right text-xs whitespace-nowrap tabular-nums", late ? "font-medium text-destructive" : "text-muted-foreground")}>
          {late ? `${late} · ${text}` : text}
        </span>
      )}
    </>
  );
  if (onOpen || children)
    return (
      <div className={cn("flex min-h-12 items-center border-l-2", row.rotten ? "border-l-destructive" : "border-l-transparent")}>
        {onOpen ? (
          <button type="button" onClick={onOpen} className={cn(LIST_ROW, "min-h-12 min-w-0 flex-1 items-start gap-3 py-2")}>
            {body}
          </button>
        ) : (
          <div className="flex min-w-0 flex-1 items-start gap-3 px-3 py-2">{body}</div>
        )}
        {children && <span className="shrink-0 pr-2">{children}</span>}
      </div>
    );
  return (
    <div className={cn("flex min-h-12 items-start gap-3 border-l-2 px-3 py-2", row.rotten ? "border-l-destructive" : "border-l-transparent")}>
      {body}
    </div>
  );
}

export function Dropped({ n }: { n: number }) {
  return n > 0 ? <p className="px-1 text-xs text-muted-foreground">{`${n} more not shown: the answer would be too large.`}</p> : null;
}

/** The steps that press `done` on these rows, each at the version shown. */
export function doneSteps(view: Pick<QueueView, "done">, rows: ViewRow[]): Step[] {
  const done = view.done;
  if (!done) return [];
  return rows.map((r) => ({ op: { op: "set", id: r.id, version: r.version, fields: { [done.field]: done.value } }, target: r.id }));
}

export function Queue({ collection, view }: { collection: CollectionViews; view: QueueView }) {
  const [all, setAll] = useState(false);
  const [gone, setGone] = useState<Set<string>>(() => new Set());
  const [asking, setAsking] = useState(false);
  const editing = useEditing();
  const [seen, setSeen] = useState(view);
  if (seen !== view) {
    setSeen(view); // the view read again: what was pressed shows as it is now
    setGone(new Set());
  }
  const rows = view.rows.filter((r) => !gone.has(r.id));
  if (rows.length === 0 && (view.count === 0 || view.rows.length > 0))
    return <p className="px-1 py-2 text-sm text-muted-foreground">{view.count === 0 ? emptyWords(collection) : "None left."}</p>;
  const more = rows.length - PHONE_ROWS;
  const done = editing ? view.done : null;
  const press = async (editing: Editing, pressed: ViewRow[]) => {
    setGone((g) => new Set([...g, ...pressed.map((r) => r.id)]));
    const what = pressed.length === 1 ? `${done?.label}: ${rowWords(pressed[0]!, view.fields).title}.` : `${done?.label}: ${pressed.length}.`;
    if (!(await editing.write(collection.name, doneSteps(view, pressed), what)))
      setGone((g) => new Set([...g].filter((id) => !pressed.some((r) => r.id === id))));
  };
  return (
    <div className="flex flex-col gap-1">
      <ul className={LIST} data-testid={`queue-${view.name}`}>
        {rows.map((row, i) => (
          <li key={row.id} className={cn(i >= PHONE_ROWS && !all && "hidden sm:block")}>
            <Row row={row} fields={view.fields} onOpen={editing ? () => editing.open(collection.name, row.id) : undefined}>
              {editing && done && (
                <Button size="sm" variant="outline" aria-label={`${done.label}: ${rowWords(row, view.fields).title}`} onClick={() => void press(editing, [row])}>
                  {done.label}
                </Button>
              )}
            </Row>
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap items-center gap-1">
        {more > 0 && !all && (
          <Button size="sm" variant="ghost" className="h-11 sm:hidden" onClick={() => setAll(true)}>
            {`Show all ${view.count}`}
          </Button>
        )}
        {editing && done && rows.length > 1 && (
          <Button size="sm" variant="ghost" className="h-11 md:h-8" onClick={() => setAsking(true)}>
            {`Mark all ${rows.length}: ${done.label}`}
          </Button>
        )}
      </div>
      <Dropped n={view.dropped} />
      {editing && done && (
        <Dialog open={asking} onClose={() => setAsking(false)} title={`${done.label}: all ${rows.length}?`}>
          <ul className="flex max-h-60 flex-col gap-1 overflow-y-auto text-sm">
            {rows.map((r) => (
              <li key={r.id} dir="auto" className="break-words">
                {[rowWords(r, view.fields).title, ...rowWords(r, view.fields).rest].join(" · ")}
              </li>
            ))}
          </ul>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setAsking(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => {
                setAsking(false);
                void press(editing, rows);
              }}
            >
              {`Mark ${rows.length}`}
            </Button>
          </div>
        </Dialog>
      )}
    </div>
  );
}
