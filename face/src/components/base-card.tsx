// The platform's own commit to the Base after a turn (retinue/tobase.py): the session writes, the platform commits and
// pushes, the owner reads one line under the turn's summary. A tap opens its card — the files, the commit's diff,
// «Undo». There is nothing to press to commit.
import { lazy, Suspense, useState } from "react";
import { Database } from "@/components/icons";
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

export function BaseCardView({ card, undo = api.baseUndo, live = defaultLive, className }: Props) {
  const [busy, setBusy] = useState(false);
  const [diff, setDiff] = useState(false);

  async function pressUndo() {
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

/** The line under a turn's summary — what the turn wrote to the Base, or why it did not and what came next — and a
 * tap on it opens the commit's card. */
export function BaseLine({ id, line, calls = api, live = defaultLive }: { id?: string | null; line: string; calls?: Pick<typeof api, "baseCard">; live?: Live }) {
  const { bases } = useLiveState();
  const card = id ? bases[id] : undefined;
  const [open, setOpen] = useState(false);
  const text = card?.state === "undone" ? `${line} Undone.` : line;
  if (!id) return <p className="text-xs text-muted-foreground wrap-anywhere">
        <Database className="mr-1 inline size-3.5 align-[-2px]" />
        {text}
      </p>;
  return (
    <>
      <button
        type="button"
        className="w-fit max-w-full text-left text-xs text-muted-foreground underline-offset-4 wrap-anywhere hover:text-foreground hover:underline"
        onClick={async () => {
          if (!card) {
            try {
              put(live, (await calls.baseCard(id)).card);
            } catch (e) {
              toast(e instanceof ApiError ? e.message : "The card did not open.", "error");
              return;
            }
          }
          setOpen(true);
        }}
      >
        <Database className="mr-1 inline size-3.5 align-[-2px]" />
        {text}
      </button>
      <BaseDialog id={open ? id : null} onClose={() => setOpen(false)} />
    </>
  );
}
