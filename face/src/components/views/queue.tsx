// A `queue` view: the records a filter keeps, the longest silent first (or by its `sort`), each a line — its days of
// silence when the queue has a threshold, which way its last touch went and when (`latest`), its head, the field
// beside it once the list is wide, the line under it, its enums at the right — marked by whose move it is: a filled
// square for the owner's, an empty one for what he waits for; the days red or amber past the threshold, «overdue» said
// to a screen reader. A phone shows the first three and «Show all». Where the screen writes, a row opens its record's
// sheet, and a queue with `done` has its button on each row — «Sent» — and «Mark all N» below, asked once; a row
// pressed leaves at once.
import { Fragment, useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { ArrowDownLeft, ArrowUpRight } from "@/components/icons";
import { type CollectionViews, daysWords, emptyWords, type Labels, type Latest, lineWords, type QueueView, rowWords, valueWords, type ViewRow } from "@/lib/collections";
import { day } from "@/lib/spaces";
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

/** The row's mark: a small square filled for what is the owner's to do, an empty one for what he waits for, a quiet
 * one for a board's card. How late it is, the days beside it say. */
export function Mark({ mark }: { mark?: "act" | "wait" }) {
  return (
    <span
      aria-hidden
      className={cn(
        "size-1.5 shrink-0 rounded-[1px]",
        mark === "act" ? "bg-warning" : mark === "wait" ? "ring-1 ring-muted-foreground/60 ring-inset" : "bg-muted-foreground/40",
      )}
    />
  );
}

/** The days in a column of their own, short — «15d», «0d», «in 3d» — their words for a screen reader and a tooltip. */
export function shortDays(days: number | null): string {
  return days === null ? "" : days < 0 ? `in ${-days}d` : `${days}d`;
}

export function Row({
  row,
  fields,
  kind = "queue",
  mark,
  days = true,
  latest = false,
  labels,
  label,
  onOpen,
  children,
}: {
  row: ViewRow;
  fields: string[];
  kind?: "board" | "queue";
  /** A queue's: whose move its rows are. */
  mark?: "act" | "wait";
  /** Its days of silence in their column: a queue measured against a threshold. */
  days?: boolean;
  /** The column of its newest record that names it — a deal's last touch — when its queue has one. */
  latest?: boolean;
  labels?: Labels;
  label?: string;
  /** Opens the record's sheet: the row is a button then. */
  onOpen?: () => void;
  /** A button of its own beside the row: «Sent». */
  children?: React.ReactNode;
}) {
  const { title, beside, under, tags } = lineWords(row, fields, labels);
  const { text, late } = daysWords(row, kind);
  const side = [label, ...tags].filter((p): p is string => !!p);
  // One line on a wide screen, its cells in columns every row shares — the head, the field beside it once the list is
  // wide enough, the line under it — two lines on a phone.
  const body = (
    <>
      <Mark mark={mark} />
      {days && (
        <span
          title={[text, late].filter(Boolean).join(", ") || undefined}
          className={cn(
            "w-10 shrink-0 text-right font-mono text-xs tabular-nums",
            late ? (mark === "wait" ? "text-amber-700 dark:text-warning" : "text-destructive") : "text-muted-foreground",
          )}
        >
          {shortDays(row.days)}
          {(text || late) && <span className="sr-only">{`: ${[text, late].filter(Boolean).join(", ")}`}</span>}
        </span>
      )}
      {latest && <LatestCell latest={row.latest} />}
      <span className="flex min-w-0 flex-1 flex-col sm:flex-row sm:items-baseline sm:gap-3">
        <span dir="auto" title={beside ? `${title} · ${beside}` : undefined} className="truncate text-[15px] font-medium leading-snug sm:w-44 sm:shrink-0 md:text-sm">
          {title}
        </span>
        {beside && (
          <span dir="auto" className="hidden w-64 shrink-0 truncate text-[13px] text-muted-foreground @4xl:block">
            {beside}
          </span>
        )}
        {under && (
          <span dir="auto" className="min-w-0 flex-1 truncate text-sm leading-snug text-muted-foreground md:text-[13px]">
            {under}
          </span>
        )}
      </span>
      {side.length > 0 && (
        <span className="hidden max-w-40 shrink-0 truncate text-right text-xs text-muted-foreground sm:inline">
          <Parts parts={side} />
        </span>
      )}
    </>
  );
  if (onOpen || children)
    return (
      <div className="flex min-h-10 items-center">
        {onOpen ? (
          <button type="button" onClick={onOpen} className={cn(LIST_ROW, "min-h-10 min-w-0 flex-1 items-center gap-3 py-2")}>
            {body}
          </button>
        ) : (
          <div className="flex min-w-0 flex-1 items-center gap-3 px-3 py-2">{body}</div>
        )}
        {children && <span className="shrink-0 pr-2">{children}</span>}
      </div>
    );
  return <div className="flex min-h-10 items-center gap-3 px-3 py-2">{body}</div>;
}

/** Which way the newest record went and its day: ↙ towards the owner, ↗ from him. */
function LatestCell({ latest }: { latest?: Latest }) {
  if (!latest) return <span aria-hidden className="w-16 shrink-0" />;
  const Arrow = latest.arrow === "in" ? ArrowDownLeft : latest.arrow === "out" ? ArrowUpRight : null;
  const words = [latest.label ?? (latest.value !== null ? valueWords(latest.value) : null), day(latest.date)].filter(Boolean).join(" · ");
  return (
    <span title={words} className="flex w-16 shrink-0 items-center gap-1 font-mono text-xs text-muted-foreground tabular-nums">
      {Arrow ? (
        <Arrow aria-hidden className={cn("size-3.5 shrink-0", latest.arrow === "in" ? "text-warning" : "text-muted-foreground")} />
      ) : (
        <span aria-hidden className="size-3.5 shrink-0" />
      )}
      <span className="truncate">{day(latest.date)}</span>
      <span className="sr-only">{`, last: ${words}`}</span>
    </span>
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
  const latestShown = view.latest === true;
  if (rows.length === 0 && (view.count === 0 || view.rows.length > 0))
    return <p className="px-1 py-2 text-sm text-muted-foreground">{view.count === 0 ? emptyWords(collection) : "None left."}</p>;
  const more = rows.length - PHONE_ROWS;
  const done = editing ? view.done : null;
  const press = async (editing: Editing, pressed: ViewRow[]) => {
    setGone((g) => new Set([...g, ...pressed.map((r) => r.id)]));
    const what = pressed.length === 1 ? `${done?.label}: ${rowWords(pressed[0]!, view.fields, undefined, collection.labels).title}.` : `${done?.label}: ${pressed.length}.`;
    if (!(await editing.write(collection.name, doneSteps(view, pressed), what)))
      setGone((g) => new Set([...g].filter((id) => !pressed.some((r) => r.id === id))));
  };
  return (
    <div className="flex flex-col gap-1">
      <ul className={cn(LIST, "@container")} data-testid={`queue-${view.name}`}>
        {rows.map((row, i) => (
          <li key={row.id} className={cn(i >= PHONE_ROWS && !all && "hidden sm:block")}>
            <Row row={row} fields={view.fields} mark={view.mark ?? "act"} days={view.rot !== null} latest={latestShown} labels={collection.labels} onOpen={editing ? () => editing.open(collection.name, row.id) : undefined}>
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
