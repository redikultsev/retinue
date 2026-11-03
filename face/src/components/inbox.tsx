// «Today» as an inbox: one list of what waits for the owner and what goes on — a row a line or two on one grid (a mark,
// what to do, who or what, how long), «Working», «Done since you looked» and the week's dates under it — and the row
// opened whole in the pane at its right: a question with its brief and one «Answer», a card with its buttons, her row
// with what it asks and «Done», a session with its last words and a reply. Nothing opens a dialog; on a narrow screen
// the pane takes the screen and «All» comes back. J / K (or the arrows) go down and up the list, E marks her row done.
// The rows are what the app already reads — the feed (`/api/feed`), the sessions — folded here, kept nowhere.
// The list is the feed's order, by action; each row's second line names its project first (the projects themselves
// are the left panel's). The tasks are a tab of their own («Tasks»), never among what waits.
import { Link } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { ApprovalCard } from "@/components/approval-card";
import { CollectionProposalCard } from "@/components/collection-proposal-card";
import { EndingCard } from "@/components/ending-card";
import { ArrowLeft, Check, Stop } from "@/components/icons";
import { ProposalCard } from "@/components/proposal-card";
import { PushButton } from "@/components/push-card";
import { Crumb, Reply } from "@/components/reply";
import { QuestionDecide, typing } from "@/components/question-decide";
import { Markdown } from "@/components/transcript/item";
import { Button, buttonVariants } from "@/components/ui/button";
import { pressRow, WaitingPane } from "@/components/waiting-sheet";
import { RecordPage } from "@/pages/spaces";
import { ApiError, api } from "@/lib/api";
import { type Deadline, type Feed, type FeedRow, feedSource, herLine, personRow, STALE_DIGEST_S, waited } from "@/lib/feed";
import { when } from "@/lib/format";
import { live, useLiveState } from "@/lib/live";
import { useWide } from "@/lib/media";
import { splitRecord } from "@/lib/pane";
import { day } from "@/lib/spaces";
import { endingOf } from "@/lib/operator";
import { orderedItems, orderedSummaries } from "@/lib/store";
import { knownProjects, projectLabel, rowProject, sessionProject, useProjects } from "@/lib/projects";
import { dayWords, elapsed, lastLine, turnStart } from "@/lib/today";
import { toast } from "@/lib/toast";
import type { Session, Sphere } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Whose move a row is: a session stands until the owner answers (`agent`), a person or a thing waits (`person`). */
type Mark = "agent" | "person" | "none";

export type InboxItem =
  | { key: string; kind: "feed"; row: FeedRow; label: string; title: string; line: string; due: string; at: number; mark: Mark }
  | { key: string; kind: "running"; session: Session; label: string; title: string; line: string; at: number }
  | { key: string; kind: "done"; session: Session; label: string; title: string; line: string; at: number }
  | { key: string; kind: "date"; deadline: Deadline; label: string; title: string; line: string; at: number };

const DECIDE: Record<string, string> = { approval: "Allow", courier: "Run", push: "Push", proposal: "Start", collection_proposal: "Apply" };
/** What to do about a session's request, by how its turn ended. */
const ENDED: Record<string, string> = { ask: "Answer", approve: "Allow", look: "Look", step: "Step", done: "Read" };
function feedItem(row: FeedRow): InboxItem {
  if (row.action === "assistant") {
    if (row.source === "deadline") return { key: row.key, kind: "feed", row, label: "Date", title: row.essence.replace(/^[^:]{1,24}:\s*/, ""), line: row.sphere_title ?? "", due: "", at: row.since, mark: "none" };
    if (row.source === "draft") return { key: row.key, kind: "feed", row, label: "Send", title: row.title, line: row.essence, due: "", at: row.since, mark: "person" };
    const { verb, text, due } = herLine(row.essence);
    return { key: row.key, kind: "feed", row, label: verb || "Look", title: row.title, line: text, due, at: row.since, mark: "person" };
  }
  const ended = endingOf(row);
  const label = ended
    ? ENDED[ended.kind]!
    : row.action === "answer"
      ? "Answer"
      : row.action === "decide"
        ? (DECIDE[row.source] ?? "Decide")
        : row.action === "reply"
          ? "Reply"
          : "Read";
  return { key: row.key, kind: "feed", row, label, title: row.title, line: row.essence, due: "", at: row.since, mark: row.action === "read" ? "none" : "agent" };
}

export interface InboxLists {
  waiting: InboxItem[];
  running: InboxItem[];
  done: InboxItem[];
  dates: InboxItem[];
}

/** The list's four parts, each thing once: a finished turn under «Done» and a base's date under «Dates», not again among
 * what waits — unless its turn ended with a word for the owner (`ending`): then it waits, with that word; a person's
 * row is Tasks' («From people»), not here. */
export function inboxLists(feed: Feed | null, running: Session[], done: Session[], deadlines: Deadline[], lines: (s: Session) => string): InboxLists {
  const rows = (feed?.groups ?? []).flatMap((g) => g.rows);
  const ended = new Set(rows.filter((r) => r.session && endingOf(r)).map((r) => r.session));
  const finished = done.filter((s) => !ended.has(s.id));
  const doneIds = new Set(finished.map((s) => s.id));
  const dateIds = new Set(deadlines.map((d) => d.id));
  return {
    waiting: rows
      .filter((r) => !personRow(r) && !(r.action === "read" && r.session && doneIds.has(r.session)) && !(r.source === "deadline" && dateIds.has(r.ref)))
      .map(feedItem),
    running: running.map((s) => ({ key: `run:${s.id}`, kind: "running", session: s, label: "Runs", title: s.title || s.track, line: lines(s), at: turnStart(s) })),
    done: finished.map((s) => ({ key: `done:${s.id}`, kind: "done", session: s, label: "Done", title: s.title || s.track, line: "", at: s.at })),
    dates: deadlines.map((d) => ({ key: `date:${d.id}`, kind: "date", deadline: d, label: "", title: d.title.replace(/^[^:]{1,24}:\s*/, ""), line: "", at: 0 })),
  };
}

function MarkBox({ mark }: { mark: Mark }) {
  if (mark === "none") return <span />;
  return (
    <span
      role="img"
      aria-label={mark === "agent" ? "a session waits for you" : "waits for you"}
      className={cn("block size-2 rounded-[2px] border border-warning", mark === "agent" && "bg-warning")}
    />
  );
}

function Row({ item, on, now, today, onPick }: { item: InboxItem; on: boolean; now: number; today: Date; onPick: () => void }) {
  const marker =
    item.kind === "feed" ? (
      <MarkBox mark={item.mark} />
    ) : item.kind === "running" ? (
      <span className="mx-auto block size-1.5 animate-pulse rounded-full bg-muted-foreground" />
    ) : item.kind === "done" ? (
      <Check aria-hidden className="-ml-0.5 block size-3 text-muted-foreground" />
    ) : (
      <span />
    );
  const date = item.kind === "date" ? dayWords(item.deadline.when, today) : null;
  const time =
    item.kind === "feed"
      ? item.row.source === "deadline"
        ? ""
        : waited(item.at, now)
      : item.kind === "running"
        ? elapsed(item.at, now)
        : item.kind === "done"
          ? when(item.at, now)
          : "";
  return (
    <li>
      <button
        type="button"
        data-testid="inbox-row"
        aria-current={on ? "true" : undefined}
        onClick={onPick}
        className={cn(
          "grid w-full grid-cols-[8px_52px_minmax(0,1fr)_auto] items-baseline gap-x-2.5 rounded-md px-2.5 py-2 text-left transition-colors focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none max-md:min-h-11",
          on ? "bg-accent" : "hover:bg-accent/50",
        )}
      >
        <span className="self-center">{marker}</span>
        <span className={cn("text-xs", date?.overdue ? "text-destructive" : date ? "text-warning-foreground dark:text-warning" : "text-muted-foreground")}>
          {date ? (date.rel === "today" || date.rel === "tomorrow" ? date.rel : day(item.kind === "date" ? item.deadline.when : "", today)) : item.label}
        </span>
        <span className={cn("truncate", item.kind === "feed" && "font-medium")}>{item.title}</span>
        <span className="font-mono text-xs text-muted-foreground tabular-nums">
          {time || (item.kind === "feed" ? item.due : "")}
        </span>
        {item.line && (
          <span className="col-span-2 col-start-3 truncate text-[13px] text-muted-foreground">
            {item.line}
            {item.kind === "feed" && item.due && time ? <span className="font-mono text-xs"> · {item.due}</span> : null}
          </span>
        )}
      </button>
    </li>
  );
}

function Part({ title, items, ...rest }: { title?: string; items: InboxItem[]; sel: string | null; now: number; today: Date; onPick: (key: string) => void }) {
  if (!items.length) return null;
  return (
    <section aria-label={title ?? "Waiting for you"} className="flex flex-col">
      {title && (
        <h2 className="grid grid-cols-[8px_minmax(0,1fr)] gap-x-2.5 px-2.5 pt-5 pb-1.5 text-xs font-normal text-muted-foreground">
          <span />
          <span>
            {title} <span className="tabular-nums">· {items.length}</span>
          </span>
        </h2>
      )}
      <ul className="flex flex-col gap-0.5">
        {items.map((item) => (
          <Row key={item.key} item={item} on={rest.sel === item.key} now={rest.now} today={rest.today} onPick={() => rest.onPick(item.key)} />
        ))}
      </ul>
    </section>
  );
}

// --- the pane -------------------------------------------------------------------------------------------------------

function where(sphere: string | null | undefined, spheres: Sphere[], fallback = ""): string {
  return spheres.find((x) => x.name === sphere)?.title ?? sphere ?? fallback;
}

/** A session's last summary — the snapshot is read once when its row opens, then the stream keeps it. */
function useLastWords(session: string): string {
  const state = useLiveState();
  useEffect(() => {
    if (!(session in live.getState().floors) && !live.isWatched(session)) live.watch(session).catch(() => undefined);
  }, [session]);
  const summaries = orderedSummaries(state.summaries[session]);
  return summaries[summaries.length - 1]?.text ?? "";
}

function SessionPane({ session, crumb, title, hint, reply }: { session: string; crumb: string; title: string; hint: string; reply: string }) {
  const words = useLastWords(session);
  return (
    <section aria-label={title} className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <Crumb session={session}>{crumb}</Crumb>
        <h2 className="text-xl font-medium text-balance">{title}</h2>
      </div>
      {words ? (
        <div className="max-w-[68ch] text-muted-foreground">
          <Markdown text={words} />
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">{hint}</p>
      )}
      <Reply session={session} placeholder={reply} />
    </section>
  );
}

function RunningPane({ session, spheres, now }: { session: Session; spheres: Sphere[]; now: number }) {
  const state = useLiveState();
  const items = orderedItems(state.items[session.id]);
  const steps = items
    .slice(-12)
    .map((i) => lastLine([i], undefined))
    .filter(Boolean)
    .slice(-4);
  const current = lastLine(items, state.deltas[session.id]);
  const [busy, setBusy] = useState(false);
  return (
    <section aria-label={session.title || session.track} className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <Crumb session={session.id}>
          {where(session.sphere, spheres, session.track)} · working for {elapsed(turnStart(session), now)}
        </Crumb>
        <h2 className="text-xl font-medium text-balance">{session.title || session.track}</h2>
      </div>
      <dl className="grid grid-cols-[88px_minmax(0,1fr)] gap-x-4 gap-y-1 rounded-lg border bg-card px-4 py-3 max-sm:grid-cols-1">
        <dt className="text-xs leading-5 text-muted-foreground">Now</dt>
        <dd className="min-w-0 break-words">{current || "thinking…"}</dd>
        {steps.length > 1 && (
          <>
            <dt className="text-xs leading-5 text-muted-foreground">Before</dt>
            <dd className="flex min-w-0 flex-col text-muted-foreground">
              {steps.slice(0, -1).map((s, n) => (
                <span key={`${n}:${s}`} className="truncate">
                  {s}
                </span>
              ))}
            </dd>
          </>
        )}
      </dl>
      <div className="flex flex-wrap gap-2">
        <Link to="/s/$id" params={{ id: session.id }} className={buttonVariants()}>
          Open the session
        </Link>
        <Button
          variant="outline"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              await api.stop(session.id);
            } catch (e) {
              toast(e instanceof ApiError ? e.message : "That didn't work.", "error");
            } finally {
              setBusy(false);
            }
          }}
        >
          <Stop />
          Stop
        </Button>
      </div>
    </section>
  );
}

/** A base's date: its day, and the Record itself under it — the same view as in Spaces, «Discuss» with it. */
function DatePane({ deadline, today }: { deadline: Deadline; today: Date }) {
  const record = deadline.path ? splitRecord(deadline.path) : null;
  const { rel, overdue } = dayWords(deadline.when, today);
  return (
    <section aria-label={deadline.title} className="flex flex-col gap-2">
      <Crumb>
        {deadline.sphere_title || deadline.sphere || "The Base"} · {deadline.title.replace(/^[^:]{1,24}:\s*/, "").replace(/^.* — /, "")} ·{" "}
        <span className={overdue ? "text-destructive" : "text-warning-foreground dark:text-warning"}>
          {day(deadline.when, today)}, {rel}
        </span>
      </Crumb>
      {record ? (
        <div className="-mx-4 md:-mx-6">
          <RecordPage space={record.space} path={record.path} pane />
        </div>
      ) : (
        <h2 className="text-xl font-medium text-balance">{deadline.title.replace(/^[^:]{1,24}:\s*/, "")}</h2>
      )}
    </section>
  );
}

function FeedPane({ row, feed, now, today }: { row: FeedRow; feed: Feed; now: number; today: Date }) {
  const refresh = () => void feedSource.refresh();
  const crumb = `${row.sphere_title || row.track || ""}${row.sphere_title || row.track ? " · " : ""}waits ${waited(row.since, now)}`;
  const ended = endingOf(row);
  if (ended)
    return (
      <section aria-label={row.title} className="flex flex-col gap-4">
        <Crumb session={row.session}>
          {row.title} · {crumb}
        </Crumb>
        <EndingCard row={row} e={ended} />
      </section>
    );
  if (row.approval?.kind === "question")
    return (
      <section aria-label={row.title} className="flex flex-col gap-4">
        <Crumb session={row.session}>
          {row.title} · {crumb}
        </Crumb>
        <QuestionDecide approval={row.approval} said={row.said} onDecided={refresh} />
      </section>
    );
  if (row.approval)
    return (
      <section aria-label={row.title} className="flex flex-col gap-4">
        <Crumb session={row.session}>
          {row.title} · {crumb}
        </Crumb>
        <ApprovalCard approval={row.approval} onDecided={refresh} />
      </section>
    );
  if (row.proposal || row.collection_proposal || row.source === "push")
    return (
      <section aria-label={row.title} className="flex flex-col gap-4">
        <Crumb session={row.session}>
          {row.title} · {crumb}
        </Crumb>
        {row.proposal && <ProposalCard proposal={row.proposal} onDecided={refresh} />}
        {row.collection_proposal && <CollectionProposalCard proposal={row.collection_proposal} onDecided={refresh} />}
        {row.source === "push" && row.session && (
          <>
            <h2 className="text-xl font-medium">{row.essence}</h2>
            <PushButton session={row.session} count={row.push ?? 0} repo={row.repos?.length === 1 ? undefined : row.repos?.[0]} />
          </>
        )}
      </section>
    );
  if ((row.action === "reply" || row.action === "read") && row.session)
    return (
      <SessionPane
        session={row.session}
        crumb={crumb}
        title={row.title}
        hint={row.essence}
        reply={row.action === "reply" ? "It waits for your message" : "Reply to go on with it"}
      />
    );
  if (row.source === "deadline") {
    const d = feed.deadlines?.find((x) => x.id === row.ref);
    return d ? <DatePane deadline={d} today={today} /> : <p className="text-sm text-muted-foreground">{row.essence}</p>;
  }
  return <WaitingPane key={row.key} row={row} bot={feed.bot} now={now} />;
}

function Pane({ item, feed, spheres, now, today }: { item: InboxItem; feed: Feed; spheres: Sphere[]; now: number; today: Date }) {
  if (item.kind === "feed") return <FeedPane row={item.row} feed={feed} now={now} today={today} />;
  if (item.kind === "running") return <RunningPane session={item.session} spheres={spheres} now={now} />;
  if (item.kind === "done")
    return (
      <SessionPane
        key={item.session.id}
        session={item.session.id}
        crumb={`${where(item.session.sphere, spheres, item.session.track)} · finished ${when(item.session.at, now)}`}
        title={item.title}
        hint="Its last words are loading…"
        reply="Reply to go on with it"
      />
    );
  return <DatePane deadline={item.deadline} today={today} />;
}

// --- the inbox ------------------------------------------------------------------------------------------------------

/** Each row's second line begins with its project («Work · …», a sub-project «Work / Site · …»): the list is by
 * action, the projects are the left panel's — the row still says where it is from. */
export function withProjects(lists: InboxLists, label: (item: InboxItem) => string): InboxLists {
  const one = (item: InboxItem): InboxItem => {
    const where = label(item);
    return where ? { ...item, line: item.line ? `${where} · ${item.line}` : where } : item;
  };
  return { waiting: lists.waiting.map(one), running: lists.running.map(one), done: lists.done.map(one), dates: lists.dates.map(one) };
}

export function Inbox({
  feed,
  running,
  done,
  spheres,
  now,
  head,
}: {
  feed: Feed | null;
  running: Session[];
  done: Session[];
  spheres: Sphere[];
  now: number;
  /** Above the list: the way it is shown. */
  head?: React.ReactNode;
}) {
  const state = useLiveState();
  const today = new Date(now * 1000);
  const nodes = useProjects();
  const lists = useMemo(() => {
    const known = knownProjects(nodes, spheres);
    const where = (item: InboxItem) =>
      projectLabel(
        item.kind === "feed"
          ? rowProject(item.row, known)
          : item.kind === "date"
            ? (item.deadline.sphere ?? "")
            : sessionProject(item.session, known),
        nodes,
        spheres,
      );
    return withProjects(
      inboxLists(feed, running, done, feed?.deadlines ?? [], (s) => lastLine(orderedItems(state.items[s.id]), state.deltas[s.id])),
      where,
    );
  }, [feed, running, done, state.items, state.deltas, nodes, spheres]);
  const waitingNow = lists.waiting;
  const all = [...waitingNow, ...lists.running, ...lists.done, ...lists.dates];
  const wide = useWide();
  const [sel, setSel] = useState<string | null>(null);
  const [opened, setOpened] = useState(false); // a narrow screen: the pane over the list
  const at = useRef(0);
  // the row picked, or — once it is gone (answered, closed) — the one now in its place
  const index = sel ? all.findIndex((i) => i.key === sel) : -1;
  const current = index >= 0 ? all[index] : (all[Math.min(at.current, all.length - 1)] ?? null);
  if (index >= 0) at.current = index;
  const key = current?.key ?? null;

  const keys = useRef({ all, key });
  keys.current = { all, key };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (typing(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
      const { all, key } = keys.current;
      const i = all.findIndex((x) => x.key === key);
      const step = e.key === "j" || e.key === "ArrowDown" ? 1 : e.key === "k" || e.key === "ArrowUp" ? -1 : 0;
      if (step && all.length) {
        e.preventDefault();
        setSel(all[Math.max(0, Math.min(all.length - 1, i + step))]!.key);
      } else if (e.key === "e") {
        const item = all[i];
        if (item?.kind === "feed" && item.row.t) void pressRow(item.row.t, "done", "Done");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const pick = (k: string) => {
    setSel(k);
    setOpened(true);
  };
  const stale = feed?.assistant_at != null && now - feed.assistant_at > STALE_DIGEST_S;
  const list = (
    <nav aria-label="Today" className="min-h-0 overflow-y-auto p-2 pb-16">
      {head && <div className="px-0.5 pt-1 pb-2">{head}</div>}
      {!feed ? (
        <p className="px-2.5 py-2 text-sm text-muted-foreground">Loading…</p>
      ) : (
        <>
          {lists.waiting.length === 0 && <p className="px-2.5 py-2 text-sm text-muted-foreground">Nothing is waiting for you.</p>}
          <Part items={waitingNow} sel={key} now={now} today={today} onPick={pick} />
          <Part title="Working" items={lists.running} sel={key} now={now} today={today} onPick={pick} />
          <Part title="Done since you looked" items={lists.done} sel={key} now={now} today={today} onPick={pick} />
          <Part title="Dates" items={lists.dates} sel={key} now={now} today={today} onPick={pick} />
          {(feed.closed_today || feed.expired_week || stale) && (
            <p className="px-2.5 pt-6 text-xs text-muted-foreground">
              {stale ? `The assistant's list is from ${when(feed.assistant_at ?? 0, now)}. ` : ""}
              Closed today {feed.closed_today ?? 0} · ran out unmarked this week {feed.expired_week ?? 0}
            </p>
          )}
        </>
      )}
    </nav>
  );
  const pane =
    current && feed ? (
      <div className="min-h-0 overflow-y-auto px-4 pt-5 pb-16 md:px-8 lg:px-10">
        <div className="mx-auto flex max-w-[720px] flex-col gap-4 lg:mx-0">
          {!wide && (
            <button type="button" className="hit -ml-1 inline-flex h-8 w-fit items-center gap-1 text-sm text-muted-foreground" onClick={() => setOpened(false)}>
              <ArrowLeft className="size-4" />
              All
            </button>
          )}
          <Pane key={current.key} item={current} feed={feed} spheres={spheres} now={now} today={today} />
        </div>
      </div>
    ) : (
      <div />
    );
  if (!wide) return opened && current ? pane : list;
  return (
    <div className="grid min-h-0 flex-1 grid-cols-[360px_minmax(0,1fr)] xl:grid-cols-[400px_minmax(0,1fr)]">
      <div className="flex min-h-0 flex-col border-r">{list}</div>
      {pane}
    </div>
  );
}
