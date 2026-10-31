// «To the Base» (retinue/tobase.py): a session's commit card — the Base files its runs changed, each a toggle, the commit's
// subject, «Commit», the diff, «Cancel». It is the same card as its Telegram copies: a change here rewrites them,
// and one there comes here on the stream. Nothing commits without a press; a press is taken once in either place.
import { lazy, Suspense, useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { ApiError, api } from "@/lib/api";
import { type Live, live as defaultLive, useLiveState } from "@/lib/live";
import { toast } from "@/lib/toast";
import type { BaseCard as Card, BaseResponse } from "@/lib/types";
import { cn } from "@/lib/utils";

// The diff renderer (@pierre/diffs and Shiki) is loaded only when the diff is opened.
const BaseDiff = lazy(() => import("@/components/base-diff").then((m) => ({ default: m.BaseDiff })));

type Calls = Pick<typeof api, "baseToggle" | "baseSubject" | "baseCommit" | "baseCancel" | "baseOpen">;

const STATE_WORDS: Record<string, string> = {
  committing: "Committing…",
  committed: "Committed",
  failed: "Not committed",
  cancelled: "Cancelled",
  stale: "Stale",
  expired: "Expired",
};

/** «3 files» */
export function filesWord(n: number): string {
  return `${n} ${n === 1 ? "file" : "files"}`;
}

function put(live: Live, card: Card | null) {
  if (card) live.dispatch({ type: "event", id: null, event: { kind: "base", data: card } });
}

interface Props {
  card: Card;
  calls?: Calls;
  live?: Live;
  className?: string;
}

export function BaseCardView({ card, calls = api, live = defaultLive, className }: Props) {
  const [busy, setBusy] = useState(false);
  const [subject, setSubject] = useState(card.subject);
  const [diff, setDiff] = useState(false);
  useEffect(() => setSubject(card.subject), [card.subject]);
  const shown = card.state === "shown";
  const chosen = card.files.filter((f) => f.on).length;

  async function act(call: () => Promise<BaseResponse>, fallback: string): Promise<boolean> {
    setBusy(true);
    try {
      const res = await call();
      put(live, res.card);
      if (res.toast) toast(res.toast);
      return true;
    } catch (e) {
      toast(e instanceof ApiError ? e.message : fallback, "error");
      return false;
    } finally {
      setBusy(false);
    }
  }

  /** «Commit» with the subject as the field shows it: an edit not saved yet is saved first. */
  async function commit() {
    const edited = subject.trim() && subject !== card.subject;
    if (edited && !(await act(() => calls.baseSubject(card.id, subject), "The subject was not saved."))) return;
    await act(() => calls.baseCommit(card.id), "The commit did not go through.");
  }

  return (
    <section data-testid="base-card" aria-label={card.view.head[0] ?? "To the Base"} className={cn("flex flex-col gap-3", className)}>
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sm text-muted-foreground">{card.view.head[0]}</span>
        {!shown && <Badge variant={card.state === "committing" ? "warning" : "secondary"}>{STATE_WORDS[card.state] ?? card.state}</Badge>}
      </div>
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (subject.trim() && subject !== card.subject) void act(() => calls.baseSubject(card.id, subject), "The subject was not saved.");
        }}
      >
        <Input aria-label="Commit subject" value={subject} disabled={!shown || busy} onChange={(e) => setSubject(e.target.value)} />
        {shown && subject !== card.subject && subject.trim() && (
          <Button type="submit" variant="outline" disabled={busy}>
            Save
          </Button>
        )}
      </form>
      <ul className="flex flex-col gap-1" aria-label="Files">
        {card.files.map((f) => (
          <li key={f.path}>
            <label className={cn("flex min-h-9 items-center gap-2 rounded-md px-1 text-sm", shown && "hover:bg-accent")}>
              <input
                type="checkbox"
                className="size-4 shrink-0"
                checked={f.on}
                disabled={!shown || busy}
                aria-label={f.path}
                onChange={(e) => void act(() => calls.baseToggle(card.id, f.path, e.target.checked), "Did not switch.")}
              />
              <span className="w-4 shrink-0 font-mono text-xs text-muted-foreground">{f.status}</span>
              <span className="min-w-0 flex-1 truncate font-mono text-[13px]" title={f.path}>
                {f.path}
              </span>
              {f.nobody && (
                <span className="shrink-0 text-xs text-muted-foreground" title="No session changed it">
                  nobody's
                </span>
              )}
              {f.shared && (
                <span className="shrink-0 text-xs text-warning-foreground" title="The file was also changed in another session">
                  ⇄
                </span>
              )}
              {f.add !== null && (
                <span className="shrink-0 font-mono text-xs tabular-nums">
                  <span className="text-green-600 dark:text-green-400">+{f.add}</span>{" "}
                  <span className="text-red-600 dark:text-red-400">−{f.del}</span>
                </span>
              )}
            </label>
          </li>
        ))}
      </ul>
      {card.files.some((f) => f.shared) && (
        <p className="text-xs text-muted-foreground">⇄ — the file was also changed in another session: decide whose it is.</p>
      )}
      {card.files.some((f) => f.nobody) && (
        <p className="text-xs text-muted-foreground">
          “nobody's” — changed, but by no session. Off: turn it on to commit it.
        </p>
      )}
      {card.others.files > 0 && (
        <p className="text-xs text-muted-foreground">
          {filesWord(card.others.files)} more not committed in other sessions ({card.others.sessions}).
        </p>
      )}
      {card.said && <p role="status" className="text-sm whitespace-pre-line">{card.said}</p>}
      <div className="flex flex-wrap gap-2">
        {shown && (
          <Button disabled={busy || chosen === 0} onClick={() => void commit()}>
            Commit · {chosen}
          </Button>
        )}
        <Button variant="outline" aria-expanded={diff} onClick={() => setDiff(!diff)}>
          {diff ? "Hide diff" : "Show diff"}
        </Button>
        {shown && (
          <Button variant="ghost" disabled={busy} onClick={() => void act(() => calls.baseCancel(card.id), "Did not cancel.")}>
            Cancel
          </Button>
        )}
      </div>
      {diff && (
        <Suspense fallback={<p className="text-sm text-muted-foreground">Loading the diff…</p>}>
          <BaseDiff id={card.id} on={card.files.filter((f) => f.on).map((f) => f.path).join("\n")} />
        </Suspense>
      )}
    </section>
  );
}

/** «To the Base»: the session's card, opened or brought up to date by the platform. Its id, or null when there is
 * nothing to commit (said as a toast). */
export async function openBase(session: string, calls: Pick<Calls, "baseOpen"> = api, live: Live = defaultLive) {
  try {
    const res = await calls.baseOpen(session);
    put(live, res.card);
    if (!res.card) toast(res.toast || "Nothing to commit.");
    return res.card?.id ?? null;
  } catch (e) {
    toast(e instanceof ApiError ? e.message : "The card did not open.", "error");
    return null;
  }
}

/** The card in a dialog, as the stream keeps it. */
export function BaseDialog({ id, onClose }: { id: string | null; onClose: () => void }) {
  const { bases } = useLiveState();
  const card = id ? bases[id] : undefined;
  return (
    <Dialog open={id !== null} onClose={onClose} title="To the Base" className="max-w-2xl">
      {card && <BaseCardView card={card} />}
      <div className="flex justify-end">
        <Button variant="ghost" onClick={onClose}>
          Close
        </Button>
      </div>
    </Dialog>
  );
}

/** «To the Base · N»: under a session's last summary, in its feed row. */
export function BaseButton({ session, count, calls = api, className }: { session: string; count: number; calls?: Pick<Calls, "baseOpen">; className?: string }) {
  const [id, setId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <>
      <Button
        variant="outline"
        size="sm"
        className={cn("w-fit", className)}
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setId(await openBase(session, calls));
          setBusy(false);
        }}
      >
        To the Base · {count}
      </Button>
      <BaseDialog id={id} onClose={() => setId(null)} />
    </>
  );
}
