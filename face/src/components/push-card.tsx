// «Push» (retinue/topush.py): a repository's push card — the session's branch and the remote branch it goes to,
// the PR's title, the commits and files, what is not committed, «Push», the diff, «Cancel». It is the same card
// as its Telegram copies: a change here rewrites them, and one there comes here on the stream. Nothing is pushed
// without a press; a press is taken once in either place, and never into the default branch.
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { ApiError, api } from "@/lib/api";
import { type Live, live as defaultLive, useLiveState } from "@/lib/live";
import { toast } from "@/lib/toast";
import type { PushCard as Card, PushResponse } from "@/lib/types";
import { cn } from "@/lib/utils";

type Calls = Pick<typeof api, "pushTarget" | "pushTitle" | "pushSend" | "pushCancel" | "pushOpen" | "pushDiff">;

const STATE_WORDS: Record<string, string> = {
  pushing: "Pushing…",
  pushed: "Pushed",
  cancelled: "Cancelled",
  stale: "Stale",
  expired: "Expired",
};

/** «3 commits» */
export function commitsWord(n: number): string {
  return `${n} ${n === 1 ? "commit" : "commits"}`;
}

function put(live: Live, card: Card | null) {
  if (card) live.dispatch({ type: "event", id: null, event: { kind: "push", data: card } });
}

/** A link only to the code hosts the platform pushes to: the URL comes from GitHub's API or GitLab's answer. */
export function prLink(url: string): string | null {
  try {
    const u = new URL(url);
    return u.protocol === "https:" && (u.hostname === "github.com" || u.hostname === "gitlab.com") ? u.href : null;
  } catch {
    return null;
  }
}

interface Props {
  card: Card;
  calls?: Calls;
  live?: Live;
  className?: string;
}

export function PushCardView({ card, calls = api, live = defaultLive, className }: Props) {
  const [busy, setBusy] = useState(false);
  const [target, setTarget] = useState(card.target);
  const [title, setTitle] = useState(card.title);
  const [diff, setDiff] = useState(false);
  useEffect(() => setTarget(card.target), [card.target]);
  useEffect(() => setTitle(card.title), [card.title]);
  const shown = card.state === "shown";
  const link = card.url ? prLink(card.url) : null;

  async function act(call: () => Promise<PushResponse>, fallback: string): Promise<boolean> {
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

  /** «Push» with the fields as they show: an edit not saved yet is saved first. */
  async function send() {
    if (target.trim() && target !== card.target && !(await act(() => calls.pushTarget(card.id, target.trim()), "The branch was not saved.")))
      return;
    if (title.trim() && title !== card.title && !(await act(() => calls.pushTitle(card.id, title.trim()), "The title was not saved.")))
      return;
    await act(() => calls.pushSend(card.id), "Not pushed.");
  }

  return (
    <section data-testid="push-card" aria-label={card.view.head[0] ?? "Push"} className={cn("flex flex-col gap-3", className)}>
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sm text-muted-foreground">{card.view.head[0]}</span>
        {!shown && <Badge variant={card.state === "pushing" ? "warning" : "secondary"}>{STATE_WORDS[card.state] ?? card.state}</Badge>}
      </div>
      <label className="flex flex-col gap-1 text-sm">
        <span className="text-muted-foreground">
          Branch <span className="font-mono">{card.branch}</span> → to {card.repo}, PR into <span className="font-mono">{card.base}</span>
        </span>
        <Input aria-label="Branch" className="font-mono" value={target} disabled={!shown || busy} onChange={(e) => setTarget(e.target.value)} />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        <span className="text-muted-foreground">PR title</span>
        <Input aria-label="PR title" value={title} disabled={!shown || busy} onChange={(e) => setTitle(e.target.value)} />
      </label>
      <p className="text-xs text-muted-foreground">
        Going out: {commitsWord(card.unpushed ?? card.commits.length)}
        {card.bytes !== undefined ? `, ${sizeWord(card.bytes)}` : ""} — everything below and nothing else.
      </p>
      <ul className="flex flex-col gap-2" aria-label="Commits">
        {card.commits.map((c) => (
          <li key={c.sha} className="flex min-w-0 flex-col gap-1 text-sm">
            <div className="flex min-w-0 gap-2">
              <span className="shrink-0 font-mono text-xs text-muted-foreground">{c.sha.slice(0, 7)}</span>
              <span className="min-w-0 truncate font-medium">{c.subject}</span>
              {(c.parents?.length ?? 1) > 1 && <span className="shrink-0 text-xs text-muted-foreground">merge</span>}
            </div>
            {c.author && (
              <span className="pl-4 text-xs text-muted-foreground">
                author: {c.author}
                {c.committer && c.committer !== c.author ? `; committed by: ${c.committer}` : ""}
              </span>
            )}
            {c.message?.includes("\n") && (
              <pre className="max-h-40 overflow-auto rounded bg-muted/50 p-2 text-xs whitespace-pre-wrap">{c.message}</pre>
            )}
            {(c.files ?? []).map((f) => (
              <span key={f.path} className="truncate pl-4 font-mono text-xs" title={f.path}>
                {f.status} {f.path}
              </span>
            ))}
          </li>
        ))}
      </ul>
      <p className="text-xs text-muted-foreground">The whole PR:</p>
      <ul className="flex flex-col gap-1" aria-label="Files">
        {card.files.map((f) => (
          <li key={f.path} className="flex min-w-0 items-center gap-2 text-sm">
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
      {card.workflows.length > 0 && (
        <p className="text-sm text-warning-foreground">The token cannot push changes to .github/workflows — take them out of the branch or push yourself.</p>
      )}
      {card.dirty.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Not committed ({card.dirty.length}): {card.dirty.slice(0, 5).join(", ")}
          {card.dirty.length > 5 ? "…" : ""} — will not go into the push; ask the session to commit it.
        </p>
      )}
      {card.said && (
        <p role="status" className="text-sm whitespace-pre-line">
          {card.said}
        </p>
      )}
      {link && (
        <a href={link} target="_blank" rel="noreferrer noopener" className="text-sm text-primary underline">
          Open PR
        </a>
      )}
      <div className="flex flex-wrap gap-2">
        {shown && (
          <Button disabled={busy || card.workflows.length > 0} onClick={() => void send()}>
            Push · {commitsWord(card.ahead)}
          </Button>
        )}
        <Button variant="outline" aria-expanded={diff} onClick={() => setDiff(!diff)}>
          {diff ? "Hide diff" : "Show diff"}
        </Button>
        {shown && (
          <Button variant="ghost" disabled={busy} onClick={() => void act(() => calls.pushCancel(card.id), "Did not cancel.")}>
            Cancel
          </Button>
        )}
      </div>
      {diff && <CommitsDiff id={card.id} tip={card.commits[0]?.sha ?? ""} load={calls.pushDiff} />}
    </section>
  );
}

/** «3.2 MB» */
function sizeWord(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** What a push carries, commit by commit with each one's message and diff, as text: never rendered as anything. */
function CommitsDiff({ id, tip, load }: { id: string; tip: string; load: Calls["pushDiff"] }) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: a new tip is a new diff
  useEffect(() => {
    let gone = false;
    load(id)
      .then((d) => !gone && setText(d.truncated ? `${d.diff}\n… the diff is over 1 MB — showing the start.` : d.diff))
      .catch((e) => !gone && setError(e instanceof ApiError ? e.message : "The diff did not load."));
    return () => {
      gone = true;
    };
  }, [id, tip, load]);
  if (error) return <p className="text-sm text-destructive">{error}</p>;
  if (text === null) return <p className="text-sm text-muted-foreground">Loading the diff…</p>;
  return (
    <pre data-testid="push-diff" className="max-h-[60vh] overflow-auto rounded-lg border p-2 font-mono text-xs whitespace-pre-wrap">
      {text}
    </pre>
  );
}

/** «Push»: the session's card, opened or brought up to date by the platform. Its id, or null when there is
 * nothing to push (said as a toast). */
export async function openPush(session: string, repo?: string, calls: Pick<Calls, "pushOpen"> = api, live: Live = defaultLive) {
  try {
    const res = await calls.pushOpen(session, repo);
    put(live, res.card);
    if (!res.card) toast(res.toast || "Nothing to push.");
    return res.card?.id ?? null;
  } catch (e) {
    toast(e instanceof ApiError ? e.message : "The card did not open.", "error");
    return null;
  }
}

/** The card in a dialog, as the stream keeps it. */
export function PushDialog({ id, onClose }: { id: string | null; onClose: () => void }) {
  const { pushes } = useLiveState();
  const card = id ? pushes[id] : undefined;
  return (
    <Dialog open={id !== null} onClose={onClose} title="Push" className="max-w-2xl">
      {card && <PushCardView card={card} />}
      <div className="flex justify-end">
        <Button variant="ghost" onClick={onClose}>
          Close
        </Button>
      </div>
    </Dialog>
  );
}

/** «Push · N»: under a session's last summary, in its feed row, in its menu. */
export function PushButton({
  session,
  count,
  repo,
  calls = api,
  className,
}: {
  session: string;
  count: number;
  repo?: string;
  calls?: Pick<Calls, "pushOpen">;
  className?: string;
}) {
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
          setId(await openPush(session, repo, calls));
          setBusy(false);
        }}
      >
        Push{repo ? ` ${repo}` : ""} · {count}
      </Button>
      <PushDialog id={id} onClose={() => setId(null)} />
    </>
  );
}
