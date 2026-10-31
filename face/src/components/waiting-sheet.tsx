// A row of «From the assistant» opened: what it is about and what to do with it. What the digest already says — who,
// the sphere, how long it waits, her one line — shows at once; the rest only the router has (her judgement whole, the
// deadline, the letter's thread or the chat, a draft's text), so the sheet asks for it (`api.waitingDetail`) and fills
// in a moment later. Nothing of it is kept: not by the platform, not here once the sheet closes. The actions go where
// the thing is — the Gmail thread, the person's chat, her card in Telegram, where sending and deciding stay — and
// «Done», «Not a task», «Tomorrow» close the row as on the row itself.
import { useEffect, useState } from "react";
import { AlertCircle, ArrowUpRight, Ban, Check, Clock } from "@/components/icons";
import { Button, buttonVariants } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { ApiError, api } from "@/lib/api";
import { type DetailAsk, type DetailLink, type FeedRow, feedSource, linkOk, type Press, type WaitingDetail, waited } from "@/lib/feed";
import { when } from "@/lib/format";
import { openOutside } from "@/lib/telegram";
import { toast } from "@/lib/toast";
import { cn } from "@/lib/utils";

export const PRESSES: [Press, string][] = [
  ["done", "Done"],
  ["dismissed", "Not a task"],
  ["later", "Tomorrow"],
];
const UNDO_MS = 10_000;
const PRESS_FAILED = "It did not go through — try again.";

/** «Done», «Not a task», «Tomorrow» on her row: the row leaves the feed at once (`FeedSource.hide`), the router marks
 * what the row's token names, and the toast offers «Undo» for ten seconds. False when the press did not go through. */
export async function pressRow(t: string, action: Press, label: string, press = api.waitingPress): Promise<boolean> {
  const back = async () => {
    try {
      await press(t, "open");
      feedSource.unhide(t);
    } catch {
      toast(PRESS_FAILED, "error");
    }
  };
  feedSource.hide(t);
  try {
    await press(t, action);
  } catch {
    feedSource.unhide(t);
    toast(PRESS_FAILED, "error");
    return false;
  }
  toast(label, "info", UNDO_MS, undefined, { label: "Undo", run: () => void back() });
  return true;
}

const NEED_WORDS: Record<string, string> = { reply: "Reply", decide: "Decide", pay: "Pay", attend: "Attend", act: "Do" };
/** What the owner does about it, by her most pressing ask: one line, the buttons under it do it. */
const NEXT: Record<string, string> = {
  reply: "Answer them yourself, or ask the assistant for a draft.",
  decide: "Decide, then tell them — or tell the assistant, and she drafts it.",
  pay: "Pay as the message says, then mark it done.",
  attend: "Put it in your calendar, or say you can't come.",
  act: "Do what it asks, then mark it done.",
};
const KIND_WORDS: Record<string, string> = { letter: "Letter", chat: "Telegram", draft: "Draft" };

type Loaded = { state: "loading" } | { state: "ok"; detail: WaitingDetail } | { state: "error"; status: number; message: string };

/** The sheet's part only the router has, asked for while the sheet is open; again on «Try again». */
export function useDetail(row: FeedRow | null, load = api.waitingDetail): [Loaded | null, () => void] {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [attempt, setAttempt] = useState(0);
  const asks = !!row && !row.private && (!!row.t || row.source === "draft");
  // biome-ignore lint/correctness/useExhaustiveDependencies: one request per opened row and per «Try again»
  useEffect(() => {
    if (!row || !asks) {
      setLoaded(null);
      return;
    }
    let current = true;
    setLoaded({ state: "loading" });
    load(row.ref, row.t)
      .then((answer) => {
        if (!current) return;
        if (answer?.detail && Array.isArray(answer.detail.asks)) setLoaded({ state: "ok", detail: answer.detail });
        else setLoaded({ state: "error", status: 502, message: "The assistant's side could not say — try again." });
      })
      .catch((e: unknown) => {
        if (!current) return;
        const status = e instanceof ApiError ? e.status : 0;
        setLoaded({ state: "error", status, message: e instanceof Error ? e.message : String(e) });
        if (status === 410) void feedSource.refresh();
      });
    return () => {
      current = false;
    };
  }, [row?.key, row?.t, asks, attempt]);
  return [loaded, () => setAttempt((n) => n + 1)];
}

/** A link out — the thread, the chat, the bot — as a button: a new tab in a browser, Telegram's own way in the Mini App. */
function Out({ link, primary = false }: { link: DetailLink; primary?: boolean }) {
  return (
    <a
      href={link.url}
      target="_blank"
      rel="noopener noreferrer"
      className={cn(buttonVariants({ variant: primary ? "default" : "outline" }), "w-full sm:w-auto")}
      onClick={(event) => {
        if (openOutside(link.url)) event.preventDefault();
      }}
    >
      {link.label}
      <ArrowUpRight />
    </a>
  );
}

function Ask({ ask, now, single, link }: { ask: DetailAsk; now: number; single: boolean; link: boolean }) {
  return (
    <li className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
        <span className="rounded-full bg-muted px-2 py-0.5 font-medium text-foreground">{NEED_WORDS[ask.need] ?? ask.need}</span>
        {ask.deadline && <span className="font-medium text-amber-700 dark:text-warning">deadline {ask.deadline}</span>}
        <span className="ml-auto text-muted-foreground tabular-nums">{when(ask.at, now)}</span>
      </div>
      {ask.text ? (
        <p className={cn("text-sm break-words whitespace-pre-line", single ? "text-foreground" : "text-muted-foreground")}>{ask.text}</p>
      ) : null}
      {link && linkOk(ask.open) && (
        <a
          href={ask.open.url}
          target="_blank"
          rel="noopener noreferrer"
          className="w-fit text-xs text-primary underline-offset-4 hover:underline"
          onClick={(event) => {
            if (openOutside(ask.open?.url ?? "")) event.preventDefault();
          }}
        >
          {ask.open.label === "Open in Gmail" ? "This letter in Gmail" : ask.open.label}
        </a>
      )}
    </li>
  );
}

function Skeleton() {
  return (
    <div aria-hidden="true" className="flex animate-pulse flex-col gap-2">
      <div className="h-3 w-24 rounded bg-muted" />
      <div className="h-3 w-full rounded bg-muted" />
      <div className="h-3 w-4/5 rounded bg-muted" />
    </div>
  );
}

function Body({ row, loaded, retry, now }: { row: FeedRow; loaded: Loaded | null; retry: () => void; now: number }) {
  if (row.private)
    return (
      <p className="text-sm text-muted-foreground">
        Private: what it says and its buttons are behind «Show» in Telegram, and the app never asks for them.
      </p>
    );
  if (!loaded || loaded.state === "loading")
    return (
      <div className="flex flex-col gap-3" aria-busy="true">
        <p className="text-sm text-muted-foreground">{row.essence}</p>
        <Skeleton />
        <p className="text-xs text-muted-foreground" role="status">
          Asking the assistant…
        </p>
      </div>
    );
  if (loaded.state === "error")
    return (
      <div className="flex flex-col gap-3">
        <p className="text-sm text-muted-foreground">{row.essence}</p>
        <div role="alert" className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 p-3 text-sm">
          <AlertCircle className="mt-0.5 size-4 shrink-0" />
          <span className="min-w-0 flex-1">
            {loaded.status === 410 ? "This row is out of date — the list is being refreshed." : loaded.message}
          </span>
          {loaded.status !== 410 && (
            <Button variant="outline" size="sm" onClick={retry}>
              Try again
            </Button>
          )}
        </div>
      </div>
    );
  const { detail } = loaded;
  const many = detail.asks.length > 1;
  return (
    <div className="flex flex-col gap-4">
      {detail.draft && (
        <section aria-label="The draft" className="flex flex-col gap-2">
          <h3 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">The draft</h3>
          {detail.draft.subject && <p className="text-sm font-medium break-words">{detail.draft.subject}</p>}
          <p className="max-h-64 overflow-y-auto rounded-md border bg-muted/40 p-3 text-sm break-words whitespace-pre-wrap">
            {detail.draft.text}
          </p>
          <p className="text-xs text-muted-foreground">
            {detail.draft.state === "link"
              ? "Telegram sends it only from your own account: open her card and send it by its link."
              : `Waits for «Send» on her card in Telegram until ${when(detail.draft.expires, now)}.`}
          </p>
        </section>
      )}
      {detail.asks.length > 0 && (
        <section aria-label={detail.draft ? "What it answers" : "What it asks"} className="flex flex-col gap-2">
          <h3 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
            {detail.draft ? "What it answers" : many ? `What they ask (${detail.asks.length + detail.more})` : "What they ask"}
          </h3>
          <ul className="flex flex-col gap-3">
            {detail.asks.map((ask, n) => (
              <Ask key={n} ask={ask} now={now} single={!many} link={many && ask.open?.url !== detail.open?.url} />
            ))}
          </ul>
          {detail.more > 0 && <p className="text-xs text-muted-foreground">And {detail.more} older — in Telegram.</p>}
        </section>
      )}
      {!detail.draft && detail.asks[0] && NEXT[detail.asks[0].need] && (
        <p className="text-sm text-muted-foreground">{NEXT[detail.asks[0].need]}</p>
      )}
      {!linkOk(detail.open) && detail.kind === "chat" && (
        <p className="text-xs text-muted-foreground">They have no @username: open the chat with them in Telegram yourself.</p>
      )}
    </div>
  );
}

export function WaitingSheet({
  row,
  bot,
  onClose,
  now = Date.now() / 1000,
  load,
}: {
  row: FeedRow | null;
  bot: string;
  onClose: () => void;
  now?: number;
  load?: typeof api.waitingDetail;
}) {
  const [loaded, retry] = useDetail(row, load);
  const detail = loaded?.state === "ok" ? loaded.detail : null;
  // her chat with the owner: where a draft's «Send» is, and where he asks her for one
  const botLabel = row?.source === "draft" ? "Send in Telegram" : row?.private ? "Open in Telegram" : "Ask the assistant";
  const botLink: DetailLink | null = bot ? { url: `https://t.me/${bot}`, label: botLabel } : null;
  const open = linkOk(detail?.open) ? detail.open : null;
  const press = async (action: Press, label: string) => {
    if (!row?.t) return;
    onClose();
    await pressRow(row.t, action, label);
  };
  const where = row ? row.sphere_title || row.track : null;
  return (
    <Dialog open={!!row} onClose={onClose} title={row?.title ?? ""} sheet>
      {row && (
        <div className="flex flex-col">
          <div className="flex flex-col gap-4 px-4 py-4">
            <p className="flex flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground">
              <span>{KIND_WORDS[row.source] ?? row.source}</span>
              {where && (
                <>
                  <span aria-hidden="true">·</span>
                  <span>
                    {row.sphere_icon ? `${row.sphere_icon} ` : ""}
                    {where}
                  </span>
                </>
              )}
              <span aria-hidden="true">·</span>
              <span className="tabular-nums">waits {waited(row.since, now)}</span>
              {detail?.account && (
                <>
                  <span aria-hidden="true">·</span>
                  <span className="min-w-0 truncate">to {detail.account}</span>
                </>
              )}
            </p>
            <Body row={row} loaded={loaded} retry={retry} now={now} />
          </div>
          <div className="sticky bottom-0 flex flex-col gap-2 border-t bg-background px-4 py-3">
            <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
              {row.source === "draft" ? (
                <>
                  {botLink && <Out link={botLink} primary />}
                  {open && <Out link={{ ...open, label: "Open the thread" }} />}
                </>
              ) : (
                <>
                  {open && <Out link={open} primary />}
                  {botLink && <Out link={botLink} primary={!open} />}
                </>
              )}
            </div>
            {row.t && !(loaded?.state === "error" && loaded.status === 410) && (
              <div className="flex flex-wrap gap-1">
                {PRESSES.map(([action, label]) => (
                  <Button key={action} variant="ghost" size="sm" onClick={() => void press(action, label)}>
                    {action === "done" ? <Check /> : action === "later" ? <Clock /> : <Ban />}
                    {label}
                  </Button>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </Dialog>
  );
}
