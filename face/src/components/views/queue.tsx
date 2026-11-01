// A `queue` view: the records a filter keeps, the longest silent first, each with its days of silence — and past the
// form's threshold «overdue», in a word as well as in red. A phone shows the first three and «Show all».
import { Fragment, useState } from "react";
import { Button } from "@/components/ui/button";
import { type CollectionViews, daysWords, emptyWords, type QueueView, rowWords, type ViewRow } from "@/lib/collections";
import { cn, LIST } from "@/lib/utils";

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

export function Row({ row, fields, kind = "queue", label }: { row: ViewRow; fields: string[]; kind?: "board" | "queue"; label?: string }) {
  const { title, rest } = rowWords(row, fields);
  const { text, late } = daysWords(row, kind);
  return (
    <div className={cn("flex min-h-12 items-start gap-3 border-l-2 px-3 py-2", row.rotten ? "border-l-destructive" : "border-l-transparent")}>
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
    </div>
  );
}

export function Dropped({ n }: { n: number }) {
  return n > 0 ? <p className="px-1 text-xs text-muted-foreground">{`${n} more not shown: the answer would be too large.`}</p> : null;
}

export function Queue({ collection, view }: { collection: CollectionViews; view: QueueView }) {
  const [all, setAll] = useState(false);
  if (view.rows.length === 0 && view.count === 0) return <p className="px-1 py-2 text-sm text-muted-foreground">{emptyWords(collection)}</p>;
  const more = view.rows.length - PHONE_ROWS;
  return (
    <div className="flex flex-col gap-1">
      <ul className={LIST} data-testid={`queue-${view.name}`}>
        {view.rows.map((row, i) => (
          <li key={row.id} className={cn(i >= PHONE_ROWS && !all && "hidden sm:block")}>
            <Row row={row} fields={view.fields} />
          </li>
        ))}
      </ul>
      {more > 0 && !all && (
        <Button size="sm" variant="ghost" className="h-11 self-start sm:hidden" onClick={() => setAll(true)}>
          {`Show all ${view.count}`}
        </Button>
      )}
      <Dropped n={view.dropped} />
    </div>
  );
}
