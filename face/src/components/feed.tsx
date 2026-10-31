// «Waiting for you»: one queue grouped by what the owner does — answer a question, decide a card, reply to a session
// that waits for his word, read a finished turn, and the assistant's items — not by where it came from. Each row: where
// it came from (icon), its sphere, its title, how long it waits and its one line; the action itself in place where it
// is small (a question's buttons, «Allow» / «Deny»), so the home screen is a peek, not a way elsewhere. The
// assistant's items open a sheet (`WaitingSheet`): what the row is about, asked of the router, and the way to where it
// is — the Gmail thread, the person's chat, her card in Telegram, where sending and deciding stay; a base's date opens
// its Record. A person's row of hers closes here too — «Done», «Not a task», «Tomorrow», with «Undo» in the toast —
// and under her rows a line says how many closed today and how many ran out unmarked in the week.
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { ApprovalCard } from "@/components/approval-card";
import { BaseButton } from "@/components/base-card";
import { ProposalCard } from "@/components/proposal-card";
import { PushButton } from "@/components/push-card";
import {
  ArrowUpRight,
  Ban,
  Check,
  ChevronRight,
  Clock,
  Database,
  Fork,
  GitBranch,
  ListTodo,
  Mail,
  MessageCircle,
  MessageQuestion,
  Package,
  Pencil,
  Terminal,
} from "@/components/icons";
import { Button } from "@/components/ui/button";
import { PRESSES, pressRow, WaitingSheet } from "@/components/waiting-sheet";
import { api } from "@/lib/api";
import { type Feed as FeedData, type FeedRow, feedSource, type Press, STALE_DIGEST_S, useFeed, waited } from "@/lib/feed";
import { splitRecord, useOpenRecord } from "@/lib/pane";
import { inputText, when } from "@/lib/format";
import { useLiveState } from "@/lib/live";
import type { Approval } from "@/lib/types";
import { cn, LIST } from "@/lib/utils";

const ICONS: Record<string, (props: React.SVGProps<SVGSVGElement>) => React.ReactNode> = {
  question: MessageQuestion,
  approval: Terminal,
  courier: Package,
  session: ListTodo,
  letter: Mail,
  chat: MessageCircle,
  draft: Pencil,
  deadline: Clock,
  base: Database,
  push: GitBranch,
  proposal: Fork,
};
/** A row's small actions: muted at rest, full contrast on the row's hover or focus; never hover-only (NN/g), and a
 * 44 px target on a touch screen (`hit`). */
const ROW_ICON =
  "hit inline-flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none group-hover/row:text-foreground [&_svg]:size-4";
const SMALL_CHARS = 240;
const SMALL_LINES = 4;
const SOURCE_WORDS: Record<string, string> = {
  question: "question",
  approval: "approval",
  courier: "courier",
  session: "session",
  letter: "letter",
  chat: "Telegram",
  draft: "draft",
  deadline: "deadline",
  base: "To the Base",
  push: "Push",
  proposal: "proposed sub-session",
};

/** The card as the app knows it now: a press here or in Telegram may be newer than the feed's copy. */
function useCurrent(approval: Approval): Approval {
  const { approvals } = useLiveState();
  const live = approvals[approval.id];
  return live && live.at >= approval.at ? live : approval;
}

/** A card in place. A question, or a call short enough to read at a glance, shows whole; a longer call shows its
 * one line, and the whole card — the input as it is, both buttons — opens under it: nothing is decided unseen. */
function CardAction({ approval, essence }: { approval: Approval; essence: string }) {
  const current = useCurrent(approval);
  const text = inputText(current);
  const small = current.kind === "question" || (text.length <= SMALL_CHARS && text.split("\n").length <= SMALL_LINES);
  const [open, setOpen] = useState(small);
  if (open) return <ApprovalCard approval={current} onDecided={() => void feedSource.refresh()} />;
  return (
    <div className="flex items-start gap-3 pl-10">
      <p className="line-clamp-2 min-w-0 flex-1 font-mono text-[13px] break-all text-muted-foreground">{essence}</p>
      <Button variant="outline" size="sm" className="shrink-0" aria-expanded={false} onClick={() => setOpen(true)}>
        {current.kind === "courier" ? "Show the command" : "Show and decide"}
      </Button>
    </div>
  );
}

/** «Done», «Not a task», «Tomorrow» on her row: the row leaves the feed at once (`FeedSource.hide`), the router marks
 * what the row's token names, and the toast offers «Undo» for ten seconds. */
export function PressButtons({ t, press = api.waitingPress }: { t: string; press?: typeof api.waitingPress }) {
  const pressed = async (action: Press, label: string) => void (await pressRow(t, action, label, press));
  // One labelled action — the frequent one — and the other two as icons with their names in the tooltip (HIG: one or
  // two prominent buttons in a view; 26 rows of three outlined buttons were noise).
  const [done, doneLabel] = PRESSES[0] ?? ["done", "Done"];
  const rest = PRESSES.slice(1);
  return (
    <>
      <Button
        variant="ghost"
        size="sm"
        className="hit h-8 gap-1.5 px-2.5 text-[13px] md:h-8"
        onClick={() => void pressed(done, doneLabel)}
      >
        <Check />
        {doneLabel}
      </Button>
      {rest.map(([action, label]) => (
        <button key={action} type="button" aria-label={label} title={label} className={ROW_ICON} onClick={() => void pressed(action, label)}>
          {action === "later" ? <Clock /> : <Ban />}
        </button>
      ))}
    </>
  );
}

/** Her queue at work: how many closed today, how many ran out with no mark in the week. */
function Counts({ feed }: { feed: FeedData }) {
  if (feed.closed_today === undefined && feed.expired_week === undefined) return null;
  return (
    <p className="px-1 text-xs text-muted-foreground tabular-nums">
      Closed today: {feed.closed_today ?? 0} · Ran out unmarked this week: {feed.expired_week ?? 0}
    </p>
  );
}

/** Where a row of hers goes at a tap: a base's date to its Record; a person's row or a draft to its sheet, which asks
 * the router where the thing is. */
function Way({ row, record, onOpen }: { row: FeedRow; record: (() => void) | null; onOpen: () => void }) {
  if (row.source === "deadline")
    return record ? (
      <button type="button" aria-label="Open the Record" title="Open the Record" className={ROW_ICON} onClick={record}>
        <ArrowUpRight />
      </button>
    ) : null;
  return (
    // the row's words open the same sheet and take the tab stop; this is the pointer's way to it, at the row's end
    <button type="button" tabIndex={-1} aria-hidden="true" title="Details" className={ROW_ICON} onClick={onOpen}>
      <ChevronRight />
    </button>
  );
}

function Row({ row, now, feed, onOpen }: { row: FeedRow; now: number; feed: FeedData; onOpen: (row: FeedRow) => void }) {
  const Icon = ICONS[row.source] ?? ListTodo;
  const where = row.sphere_title || row.track;
  const openRecord = useOpenRecord();
  const path = row.source === "deadline" ? feed.deadlines?.find((d) => d.id === row.ref)?.path : null;
  const split = path ? splitRecord(path) : null;
  const record = split ? () => openRecord(split.space, split.path) : null;
  const tap = row.action !== "assistant" ? null : row.source === "deadline" ? record : () => onOpen(row);
  const title = row.session ? (
    <Link to="/s/$id" params={{ id: row.session }} className="min-w-0 truncate font-medium hover:underline">
      {row.title}
    </Link>
  ) : (
    <span className="min-w-0 truncate font-medium">{row.title}</span>
  );
  // Her rows' actions sit on the row itself, to the right: «Done», two icons, and the way to the thing.
  const actions =
    row.action === "assistant" ? (
      <div className="flex shrink-0 items-center gap-0.5 self-end sm:self-center">
        {row.t && <PressButtons t={row.t} />}
        <Way row={row} record={record} onOpen={() => onOpen(row)} />
      </div>
    ) : null;
  // spans only: inside a button (her rows) only phrasing content is valid
  const text = (
    <span className="flex min-w-0 flex-1 flex-col gap-0.5">
      <span className="flex min-w-0 items-baseline gap-2 text-[15px] md:text-sm">
        {title}
        {where && (
          <span className="hidden min-w-0 shrink truncate text-xs text-muted-foreground sm:inline">
            {row.sphere_icon ? `${row.sphere_icon} ` : ""}
            {where}
          </span>
        )}
        <span className="ml-auto shrink-0 text-xs text-muted-foreground tabular-nums">
          {row.source === "deadline" ? "" : waited(row.since, now)}
        </span>
      </span>
      {row.essence && !row.approval && !row.proposal && <span className="line-clamp-2 text-sm break-words text-muted-foreground">{row.essence}</span>}
    </span>
  );
  return (
    <li data-testid="feed-row" className="group/row flex flex-col gap-2 px-3 py-2.5 hover:bg-accent/30 focus-within:bg-accent/30">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:gap-3">
        <div className="flex min-w-0 flex-1 items-start gap-3">
          <span
            className="mt-0.5 inline-flex size-7 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground"
            title={SOURCE_WORDS[row.source] ?? row.source}
          >
            <Icon aria-label={SOURCE_WORDS[row.source] ?? row.source} />
          </span>
          {tap ? (
            // the row's words open its sheet (or a date's Record): a plain button around text, no control inside
            <button
              type="button"
              data-testid="feed-open"
              aria-haspopup={row.source === "deadline" ? undefined : "dialog"}
              className="flex min-w-0 flex-1 cursor-pointer rounded-sm text-left focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
              onClick={tap}
            >
              {text}
            </button>
          ) : (
            text
          )}
        </div>
        {actions}
      </div>
      {row.approval && <CardAction approval={row.approval} essence={row.essence} />}
      {row.proposal && <ProposalCard proposal={row.proposal} onDecided={() => void feedSource.refresh()} />}
      {row.source === "base" && row.session && <BaseButton session={row.session} count={row.base ?? 0} />}
      {row.source === "push" && row.session && (
        <PushButton session={row.session} count={row.push ?? 0} repo={row.repos?.length === 1 ? undefined : row.repos?.[0]} />
      )}
      {row.action === "reply" && row.session && (
        <Link to="/s/$id" params={{ id: row.session }} className="w-fit text-sm text-primary underline-offset-4 hover:underline">
          Open and answer
        </Link>
      )}
    </li>
  );
}

export function FeedView({
  feed,
  now = Date.now() / 1000,
  empty = "Nothing is waiting for you.",
  className,
  skip,
  heading = "h1",
}: {
  feed: FeedData | null;
  now?: number;
  empty?: string;
  className?: string;
  /** Rows shown elsewhere on the same screen (by `key`): left out here, and out of the count. */
  skip?: (row: FeedRow) => boolean;
  heading?: "h1" | "h2";
}) {
  const groups = (feed?.groups ?? [])
    .map((g) => (skip ? { ...g, rows: g.rows.filter((r) => !skip(r)) } : g))
    .filter((g) => g.rows.length > 0);
  const count = skip ? groups.reduce((n, g) => n + g.rows.length, 0) : (feed?.count ?? 0);
  const [opened, setOpened] = useState<FeedRow | null>(null);
  const Heading = heading;
  const Sub = heading === "h1" ? "h2" : "h3";
  return (
    <section aria-label="Waiting for you" className={cn("flex flex-col gap-3", className)}>
      <Heading className={cn("px-1 font-semibold", heading === "h1" ? "text-xl" : "text-base")}>
        Waiting for you {feed && count > 0 && <span className="tabular-nums">({count})</span>}
      </Heading>
      {!feed ? (
        <p className="px-1 text-sm text-muted-foreground">Loading…</p>
      ) : count === 0 ? (
        <p className="px-1 text-sm text-muted-foreground">{empty}</p>
      ) : (
        groups.map((group) => (
          <section key={group.action} aria-label={group.title} className="flex flex-col gap-2">
            <Sub className="px-1 pt-1 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
              {group.title} <span className="tabular-nums">({group.rows.length})</span>
            </Sub>
            {group.action === "assistant" && feed.assistant_at !== null && now - feed.assistant_at > STALE_DIGEST_S && (
              <p className="px-1 text-xs text-warning-foreground">The assistant's list is from {when(feed.assistant_at, now)}: no newer one has come.</p>
            )}
            <ul className={LIST}>
              {group.rows.map((row) => (
                <Row key={row.key} row={row} now={now} feed={feed} onOpen={setOpened} />
              ))}
            </ul>
            {group.action === "assistant" && <Counts feed={feed} />}
          </section>
        ))
      )}
      {feed && !groups.some((g) => g.action === "assistant") && (feed.closed_today || feed.expired_week) ? (
        <Counts feed={feed} />
      ) : null}
      <WaitingSheet row={opened} bot={feed?.bot ?? ""} now={now} onClose={() => setOpened(null)} />
    </section>
  );
}

export function Feed(props: { empty?: string; className?: string }) {
  const feed = useFeed();
  return <FeedView feed={feed} {...props} />;
}

/** The count in the header: what waits for the owner, a tap away. Nothing while nothing waits. */
export function FeedCount() {
  const feed = useFeed();
  if (!feed || feed.count === 0) return null;
  return (
    <Link
      to="/today"
      aria-label={`Waiting for you: ${feed.count}`}
      title="Waiting for you"
      className="inline-flex h-6 min-w-6 items-center justify-center rounded-full bg-warning px-2 text-xs font-semibold text-warning-foreground tabular-nums"
    >
      {feed.count}
    </Link>
  );
}
