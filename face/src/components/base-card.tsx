// The platform's own commit to the Base after a turn (retinue/tobase.py): the session writes, the platform commits and
// pushes, the owner reads one line under the turn's summary. A tap opens its card — the files, the commit's diff,
// «Undo». There is nothing to press to commit.
import { lazy, Suspense, useState } from "react";
import { ChevronRight, Database } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { ApiError, api } from "@/lib/api";
import { type Live, live as defaultLive, useLiveState } from "@/lib/live";
import { toast } from "@/lib/toast";
import type { BaseCard as Card, BaseResponse } from "@/lib/types";
import { cn } from "@/lib/utils";

const BaseDiff = lazy(() => import("@/components/base-diff").then((m) => ({ default: m.BaseDiff })));

const STATE_WORDS: Record<string, string> = {
  committing: "Writing…",
  committed: "Written",
  failed: "Not written",
  undone: "Undone",
  expired: "Closed",
};

function put(live: Live, card: Card | null) {
  if (card) live.dispatch({ type: "event", id: null, event: { kind: "base", data: card } });
}

interface Props {
  card: Card;
  undo?: (id: string) => Promise<BaseResponse>;
  live?: Live;
  className?: string;
}

/** «Undo» of a commit: the platform reverts it and the stream brings the card as it now reads. */
export function useUndo(card: Card | undefined, undo: (id: string) => Promise<BaseResponse> = api.baseUndo, live: Live = defaultLive) {
  const [busy, setBusy] = useState(false);
  async function press() {
    if (!card) return;
    setBusy(true);
    try {
      const res = await undo(card.id);
      put(live, res.card);
      if (res.toast) toast(res.toast);
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Did not undo.", "error");
    } finally {
      setBusy(false);
    }
  }
  return { busy, press };
}

export function BaseCardView({ card, undo = api.baseUndo, live = defaultLive, className }: Props) {
  const { busy, press: pressUndo } = useUndo(card, undo, live);
  const [diff, setDiff] = useState(false);

  return (
    <section data-testid="base-card" aria-label="In the Base" className={cn("flex flex-col gap-3", className)}>
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sm text-muted-foreground">{card.subject}</span>
        <Badge variant={card.state === "committing" ? "warning" : card.state === "failed" ? "destructive" : "secondary"}>
          {STATE_WORDS[card.state] ?? card.state}
        </Badge>
      </div>
      {card.said && (
        <p role="status" className="text-sm whitespace-pre-line wrap-anywhere">
          {card.said}
        </p>
      )}
      <ul className="flex flex-col gap-1" aria-label="Files">
        {card.files.map((f) => (
          <li key={f.path} className="flex min-h-9 items-center gap-2 px-1 text-sm">
            <span className="w-4 shrink-0 font-mono text-xs text-muted-foreground">{f.status}</span>
            <span className="min-w-0 flex-1 truncate font-mono text-[13px]" title={f.path}>
              {f.path}
            </span>
            {f.add !== null && (
              <span className="shrink-0 font-mono text-xs tabular-nums">
                <span className="text-green-600 dark:text-green-400">+{f.add}</span>{" "}
                <span className="text-red-600 dark:text-red-400">−{f.del}</span>
              </span>
            )}
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap gap-2">
        {card.undo && (
          <Button variant="outline" disabled={busy} onClick={() => void pressUndo()}>
            Undo
          </Button>
        )}
        <Button variant="outline" aria-expanded={diff} onClick={() => setDiff(!diff)}>
          {diff ? "Hide diff" : "Show diff"}
        </Button>
      </div>
      {diff && (
        <Suspense fallback={<p className="text-sm text-muted-foreground">Loading the diff…</p>}>
          <BaseDiff id={card.id} session={card.session} on={`${card.state}:${card.sha ?? ""}`} />
        </Suspense>
      )}
    </section>
  );
}

/** The card in a dialog, as the stream keeps it. */
export function BaseDialog({ id, onClose }: { id: string | null; onClose: () => void }) {
  const { bases } = useLiveState();
  const card = id ? bases[id] : undefined;
  return (
    <Dialog open={id !== null} onClose={onClose} title="In the Base" className="max-w-2xl">
      {card && <BaseCardView card={card} />}
      <div className="flex justify-end">
        <Button variant="ghost" onClick={onClose}>
          Close
        </Button>
      </div>
    </Dialog>
  );
}

/** What a commit wrote: its verb, its newest file — an added one before a changed one — and how many more. */
export function wroteParts(card: Card): { verb: string; first: string | null; more: number } {
  const files = [...card.files].sort((a, b) => (a.status === "A" ? 0 : 1) - (b.status === "A" ? 0 : 1));
  return { verb: card.state === "committing" ? "Writing" : "Wrote", first: files[0]?.path ?? null, more: Math.max(0, files.length - 1) };
}

/** …in a line: «Wrote travel/journal/2026-05-07-lisbon.md and 1 more». */
export function wroteWord(card: Card): string {
  const { verb, first, more } = wroteParts(card);
  if (!first) return card.said;
  return `${verb} ${first}${more ? ` and ${more} more` : ""}${card.state === "undone" ? " · undone" : ""}`;
}

/** The card of a commit, fetched when the app does not have it yet; false when it did not come. */
export async function fetchBase(id: string, calls: Pick<typeof api, "baseCard"> = api, live: Live = defaultLive): Promise<boolean> {
  try {
    put(live, (await calls.baseCard(id)).card);
    return true;
  } catch (e) {
    toast(e instanceof ApiError ? e.message : "The card did not open.", "error");
    return false;
  }
}

/** One line in the conversation where a turn wrote to the Base — «Wrote … and 1 more ›», or why it did not and what came
 * next — and a tap on it opens the commit's card: its files, its diff, «Undo». */
export function BaseLine({ id, line, calls = api, live = defaultLive }: { id?: string | null; line: string; calls?: Pick<typeof api, "baseCard">; live?: Live }) {
  const { bases } = useLiveState();
  const card = id ? bases[id] : undefined;
  const [open, setOpen] = useState(false);
  if (!id)
    return (
      <p className="flex min-h-7 items-start gap-2 text-[13px] text-muted-foreground wrap-anywhere">
        <Database className="mt-0.5 size-3.5 shrink-0" />
        {line}
      </p>
    );
  const text = card ? wroteWord(card) : line;
  return (
    <>
      <button
        type="button"
        className="-mx-1.5 flex min-h-7 w-full items-center gap-2 rounded-md px-1.5 text-left text-[13px] text-muted-foreground transition-colors hover:bg-accent/60 hover:text-foreground pointer-coarse:min-h-11"
        onClick={async () => {
          if (card || (await fetchBase(id, calls, live))) setOpen(true);
        }}
      >
        <Database className="size-3.5 shrink-0" />
        <span className="min-w-0 flex-1 truncate text-foreground/80" title={card?.said ?? line}>
          {text}
        </span>
        <ChevronRight className="size-3 shrink-0 opacity-50" />
      </button>
      <BaseDialog id={open ? id : null} onClose={() => setOpen(false)} />
    </>
  );
}
