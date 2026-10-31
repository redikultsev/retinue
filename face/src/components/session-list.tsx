// The sessions grouped by sphere (lib/spheres.ts), as Claude Code's sidebar has them: sphere → its main session first
// → what hangs under it (sub-sessions, forks, side questions), indented; a sphere and a session with rows under it fold,
// and the device remembers it. «Waiting» / «Working» / «Done» / «Pinned» are filters. A waiting session shows
// its cards under its row, with the full input and both buttons, when `cards` is set. A row opens inline (a peek):
// the session's last words and a reply box, without leaving the list. A dot marks what came since the owner looked.
import { Link } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { ApprovalCard } from "@/components/approval-card";
import { Composer } from "@/components/composer";
import { ArrowUp, ChevronDown, Clock, Columns, Database, Fork, GitPullRequest, Pin, Plus, Star } from "@/components/icons";
import { NewSession } from "@/components/new-session";
import { ReplyButtons } from "@/components/reply-buttons";
import { SessionMenu } from "@/components/session-menu";
import { Markdown } from "@/components/transcript/item";
import { ApiError, api } from "@/lib/api";
import { when } from "@/lib/format";
import { live, useLiveState } from "@/lib/live";
import { openPing } from "@/lib/replies";
import { FILTERS, type Filter, type FoldedRow, foldRows, isWaiting, sphereGroups } from "@/lib/spheres";
import { approvalsOf, lastWords, orderedItems, orderedSummaries, waitsOnOwner } from "@/lib/store";
import { toast } from "@/lib/toast";
import { QUEUED, type Session, type Sphere } from "@/lib/types";
import { cn } from "@/lib/utils";

const FILTER_KEY = "retinue-list-filter";
const FOLDED_KEY = "retinue-list-folded";
const NO_SPHERES: Sphere[] = [];
const NO_FOLDS: ReadonlySet<string> = new Set();

/** What the owner folded — session ids, and spheres as `sphere:<name>` — kept on the device. */
function useFolded(): [ReadonlySet<string>, (key: string) => void] {
  const [folded, setFolded] = useState<ReadonlySet<string>>(() => {
    try {
      const kept: unknown = JSON.parse(localStorage.getItem(FOLDED_KEY) ?? "[]");
      return new Set(Array.isArray(kept) ? kept.filter((k): k is string => typeof k === "string") : []);
    } catch {
      return new Set();
    }
  });
  function toggle(key: string) {
    const next = new Set(folded);
    if (!next.delete(key)) next.add(key);
    setFolded(next);
    try {
      localStorage.setItem(FOLDED_KEY, JSON.stringify([...next]));
    } catch {
      // storage unavailable: the fold lasts this visit
    }
  }
  return [folded, toggle];
}

function savedFilter(): Filter {
  try {
    const kept = localStorage.getItem(FILTER_KEY);
    return FILTERS.some((f) => f.value === kept) ? (kept as Filter) : "all";
  } catch {
    return "all";
  }
}

interface Props {
  /** The session open in the main panel, highlighted. */
  current?: string;
  /** Show the shown cards under «Waiting for you» (the phone's home screen). */
  cards?: boolean;
  /** Wide screens: a button that opens the session beside the current one. */
  beside?: (id: string) => void;
  onPick?: () => void;
  pin?: typeof api.pin;
  /** Fill the column it stands in: the filters stay, the list under them scrolls (the wide screen's sidebar). */
  fill?: boolean;
}

/** A session's last words and a reply box, under its row. Its snapshot is loaded when the peek opens. */
export function Peek({ id }: { id: string }) {
  const state = useLiveState();
  const [error, setError] = useState(false);
  const itemsOf = state.items[id];
  const summariesOf = state.summaries[id];
  const text = useMemo(
    () => lastWords(orderedItems(itemsOf), state.deltas[id], orderedSummaries(summariesOf)),
    [itemsOf, summariesOf, state.deltas, id],
  );
  const loaded = id in state.floors;
  useEffect(() => {
    live.watch(id).catch(() => setError(true));
  }, [id]);
  return (
    <div data-testid="peek" className="mx-1 mt-0.5 mb-1.5 flex flex-col gap-2 rounded-lg border bg-background p-2">
      <div className="max-h-48 overflow-y-auto text-sm">
        {error ? (
          <p className="text-destructive">The session did not load.</p>
        ) : !loaded ? (
          <p className="text-muted-foreground">Loading…</p>
        ) : text ? (
          <Markdown text={text} />
        ) : (
          <p className="text-muted-foreground">Nothing yet.</p>
        )}
      </div>
      <Composer session={id} running={state.sessions[id]?.state === "active"} compact />
    </div>
  );
}

/** The row's state as one small dot, as Claude's sidebar has it: waiting on the owner, working, or quiet. */
function StateDot({ s }: { s: Session }) {
  const waiting = isWaiting(s);
  return (
    <span
      aria-hidden="true"
      className={cn(
        "size-1.5 shrink-0 rounded-full",
        waiting ? "bg-warning" : s.state === "active" ? "animate-pulse bg-primary" : "bg-transparent",
      )}
    />
  );
}

/** A mini badge on the right of a row: an icon and a number, muted, with its meaning in the tooltip. */
function Mini({ title, children, className }: { title: string; children: React.ReactNode; className?: string }) {
  return (
    <span title={title} className={cn("inline-flex shrink-0 items-center gap-0.5 text-[11px] text-muted-foreground tabular-nums [&>svg]:size-3", className)}>
      {children}
      <span className="sr-only">{title}</span>
    </span>
  );
}

function Row({
  s,
  current,
  beside,
  onPick,
  pin,
  nested,
  depth = nested ? 1 : 0,
  kids = 0,
  folded = false,
  hiddenWaiting = 0,
  onFold,
}: {
  s: Session;
  current?: string;
  beside?: (id: string) => void;
  onPick?: () => void;
  pin: typeof api.pin;
  /** A side question: «side question:» before its title. */
  nested?: boolean;
  /** How far under its parent it hangs: a fork, a fork of a fork. */
  depth?: number;
  /** How many rows hang under it, and whether they are folded away. */
  kids?: number;
  folded?: boolean;
  /** Folded rows under it that wait on the owner: shown on the fold, never lost. */
  hiddenWaiting?: number;
  onFold?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const state = useLiveState();
  // Its last ping's suggested replies, when this app has seen that ping (on the stream, or in a loaded session).
  const summariesOf = state.summaries[s.id];
  const ping = useMemo(() => openPing(orderedSummaries(summariesOf)), [summariesOf]);
  // A pull request its push opened, when this app has seen the card (a loaded session, the stream).
  const pr = useMemo(
    () => Object.values(state.pushes).find((p) => p.session === s.id && p.state === "pushed" && p.url),
    [state.pushes, s.id],
  );
  async function togglePin() {
    setBusy(true);
    try {
      const res = await pin(s.id, !s.pinned);
      live.dispatch({ type: "event", id: null, event: { kind: "session", data: res.session } });
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "That didn't work.", "error");
    } finally {
      setBusy(false);
    }
  }
  const on = current === s.id;
  const action =
    "hit inline-flex size-6 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-accent hover:text-foreground [&_svg]:size-3.5";
  return (
    <li
      className={cn("group/row relative flex flex-col", depth > 0 && "border-l border-border/70")}
      style={{ marginLeft: depth ? `${depth * 1.25}rem` : undefined }}
    >
      <div
        className={cn(
          "flex h-[30px] items-center gap-1 rounded-md pr-1 pointer-coarse:h-auto pointer-coarse:min-h-11",
          depth > 0 && "ml-1",
          on ? "bg-accent" : "hover:bg-accent/70",
        )}
      >
        <Link
          to="/s/$id"
          params={{ id: s.id }}
          onClick={onPick}
          title={`${s.title || s.track} · ${s.track} · ${when(s.at)}`}
          aria-current={on ? "page" : undefined}
          className="flex min-w-0 flex-1 items-center gap-2 self-stretch pl-2"
        >
          <StateDot s={s} />
          <span className={cn("min-w-0 flex-1 truncate text-[13px] pointer-coarse:text-[15px]", s.unread ? "font-medium text-foreground" : "text-foreground/85")}>
            {s.main && (
              <>
                <Star className="mr-1 inline size-3 fill-current align-[-1px] text-primary" />
                <span className="sr-only">main: </span>
              </>
            )}
            {nested && <span className="text-muted-foreground">side question: </span>}
            {s.fork_of && !nested && <span className="text-muted-foreground">fork: </span>}
            {s.title || s.track}
          </span>
          <span className="flex shrink-0 items-center gap-1.5 pointer-fine:group-hover/row:hidden pointer-fine:group-focus-within/row:hidden">
            {s.queued && (
              <Mini title={QUEUED}>
                <Clock />
              </Mini>
            )}
            {(s.base ?? 0) > 0 && (
              <Mini title={`To the Base: ${s.base}`}>
                <Database />
                {s.base}
              </Mini>
            )}
            {pr ? (
              <Mini title={`PR open: ${pr.repo}`} className="text-success">
                <GitPullRequest />
                PR
              </Mini>
            ) : (
              (s.push ?? 0) > 0 && (
                <Mini title={`Commits not pushed: ${s.push}`}>
                  <ArrowUp />
                  {s.push}
                </Mini>
              )
            )}
            {s.waiting > 0 && (
              <span className="rounded-sm bg-warning/20 px-1 text-[11px] leading-4 font-medium text-warning-foreground dark:text-warning">
                waiting{s.waiting > 1 ? ` ${s.waiting}` : ""}
              </span>
            )}
            {s.pinned && <Pin className="size-3 fill-current text-muted-foreground pointer-coarse:hidden" />}
            {s.unread && <span role="img" aria-label="unread" title="Something new" className="size-1.5 shrink-0 rounded-full bg-primary" />}
          </span>
        </Link>
        {kids > 0 && onFold && (
          <button
            type="button"
            aria-expanded={!folded}
            aria-label={
              folded
                ? `Expand: ${s.title} (${kids}${hiddenWaiting ? `, ${hiddenWaiting} waiting` : ""})`
                : `Collapse nested: ${s.title}`
            }
            title={`${folded ? "Show" : "Collapse"} nested: ${kids}${s.children ? ` · sub-sessions open: ${s.children}` : ""}`}
            onClick={onFold}
            className={cn(
              "hit inline-flex h-6 shrink-0 items-center justify-center gap-0.5 rounded px-1 text-[11px] tabular-nums hover:bg-accent pointer-coarse:h-11 pointer-coarse:min-w-11",
              folded ? "text-foreground" : "text-muted-foreground",
            )}
          >
            <Fork className="size-3" />
            {kids}
            <ChevronDown className={cn("size-3 transition-transform", folded && "-rotate-90")} />
            {hiddenWaiting > 0 && (
              <span aria-hidden="true" className="ml-0.5 rounded-full bg-warning px-1 text-[10px] leading-4 font-semibold text-warning-foreground">
                {hiddenWaiting}
              </span>
            )}
          </button>
        )}
        {/* Row actions: on hover with a mouse, always on a touch screen (where the pin shows only once pinned). */}
        <span className="flex shrink-0 items-center gap-0.5 pointer-coarse:gap-2 pointer-fine:hidden pointer-fine:group-hover/row:flex pointer-fine:group-focus-within/row:flex">
          <button
            type="button"
            aria-pressed={!!s.pinned}
            aria-label={s.pinned ? `Unpin: ${s.title}` : `Pin: ${s.title}`}
            title={s.pinned ? "Unpin" : "Pin"}
            disabled={busy}
            onClick={togglePin}
            className={cn(action, s.pinned && "text-primary", !s.pinned && "pointer-coarse:hidden")}
          >
            <Pin className={cn(s.pinned && "fill-current")} />
          </button>
          <button
            type="button"
            aria-expanded={open}
            aria-label={open ? `Collapse: ${s.title}` : `Peek: ${s.title}`}
            title={open ? "Collapse" : "Peek"}
            onClick={() => setOpen(!open)}
            className={action}
          >
            <ChevronDown className={cn("transition-transform", open && "rotate-180")} />
          </button>
          <SessionMenu s={s} className={cn(action, "size-6 md:size-6")} />
          {beside && current && current !== s.id && (
            <button type="button" title="Open beside" aria-label={`Open beside: ${s.title}`} onClick={() => beside(s.id)} className={action}>
              <Columns />
            </button>
          )}
        </span>
      </div>
      {ping && <ReplyButtons summary={ping} className="mt-0.5 mb-1.5 ml-5" />}
      {open && <Peek id={s.id} />}
    </li>
  );
}

function Group({
  title,
  count,
  folded,
  waiting,
  onFold,
  onNew,
  children,
}: {
  title: string;
  count: number;
  folded: boolean;
  /** What in it waits on the owner: said on the folded heading. */
  waiting: number;
  onFold: () => void;
  onNew?: () => void;
  children?: React.ReactNode;
}) {
  return (
    <section className="group/sphere flex flex-col">
      <div className="flex items-center pt-2">
        <h2 className="min-w-0 flex-1">
          <button
            type="button"
            aria-expanded={!folded}
            onClick={onFold}
            className="flex h-7 w-full items-center gap-1 rounded-md px-2 text-left text-xs text-muted-foreground hover:text-foreground pointer-coarse:min-h-11"
          >
            <span className="truncate">{title}</span>
            <ChevronDown className={cn("size-3 shrink-0 transition-transform", folded && "-rotate-90")} />
            <span className="text-[11px] text-muted-foreground/70 tabular-nums">{count}</span>
            {folded && waiting > 0 && (
              <span role="note" aria-label={`${waiting} waiting`} className="ml-1 rounded-sm bg-warning/20 px-1 text-[11px] leading-4 font-medium text-warning-foreground dark:text-warning">
                {waiting} waiting
              </span>
            )}
          </button>
        </h2>
        {onNew && (
          <button
            type="button"
            aria-label={`New session: ${title}`}
            title="New session in this sphere"
            onClick={onNew}
            className="hit inline-flex size-6 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-accent hover:text-foreground pointer-fine:opacity-0 pointer-fine:group-hover/sphere:opacity-100 pointer-fine:focus-visible:opacity-100 [&_svg]:size-3.5"
          >
            <Plus />
          </button>
        )}
      </div>
      {!folded && children}
    </section>
  );
}

export function SessionList({ current, cards, beside, onPick, pin = api.pin, fill }: Props) {
  const state = useLiveState();
  const { sessions, approvals } = state;
  const spheres = live.meta?.spheres ?? NO_SPHERES;
  const [filter, setFilter] = useState<Filter>(savedFilter);
  const [starting, setStarting] = useState<string | null | undefined>(undefined); // a sphere's «＋»: its name
  const [folded, fold] = useFolded();
  const { groups } = useMemo(
    () => sphereGroups(Object.values(sessions), spheres, filter, Date.now() / 1000),
    [sessions, spheres, filter],
  );
  function pick(value: Filter) {
    setFilter(value);
    try {
      localStorage.setItem(FILTER_KEY, value);
    } catch {
      // storage unavailable: the filter lasts this visit
    }
  }
  // Under «Waiting» nothing is folded: the filter is there to see what waits.
  const folds = filter === "waiting" ? NO_FOLDS : folded;
  const row = ({ session: s, depth, side, kids, folded: shut, waiting }: FoldedRow) => (
    <div key={s.id} className="flex flex-col">
      <Row
        s={s}
        current={current}
        beside={beside}
        onPick={onPick}
        pin={pin}
        nested={side}
        depth={depth}
        kids={kids}
        folded={shut}
        hiddenWaiting={waiting}
        onFold={() => fold(s.id)}
      />
      {cards &&
        approvalsOf(approvals, s.id)
          .filter(waitsOnOwner)
          .map((a) => <ApprovalCard key={a.id} approval={a} className="mx-1 my-1" />)}
    </div>
  );
  return (
    <nav aria-label="Sessions" className={cn("flex flex-col gap-1", fill && "min-h-0 flex-1 px-2")}>
      <fieldset className="mx-1 flex shrink-0 rounded-md bg-muted p-0.5">
        <legend className="sr-only">Show</legend>
        {FILTERS.map((f) => (
          <button
            key={f.value}
            type="button"
            aria-pressed={filter === f.value}
            title={f.label}
            onClick={() => pick(f.value)}
            className={cn(
              "flex h-6 min-w-0 flex-auto items-center justify-center rounded px-1.5 text-xs pointer-coarse:h-9",
              filter === f.value ? "bg-background font-medium text-foreground shadow-xs" : "text-muted-foreground hover:text-foreground",
              f.value === "pinned" && "flex-none px-2",
            )}
          >
            {f.value === "pinned" ? (
              <>
                <Pin className="size-3" />
                <span className="sr-only">{f.label}</span>
              </>
            ) : (
              <span className="truncate">{f.label}</span>
            )}
          </button>
        ))}
      </fieldset>
      <div className={cn("flex flex-col gap-1", fill && "min-h-0 flex-1 overflow-y-auto overscroll-contain pb-4")}>
      {groups.length === 0 && (
        <p className="px-3 py-8 text-center text-sm text-muted-foreground">
          {filter === "all" ? "No sessions. Start one with “+”." : "Nothing here right now."}
        </p>
      )}
      {groups.map((g) => (
        <Group
          key={g.key}
          title={g.title}
          count={g.rows.length}
          // the session on screen keeps its sphere open
          folded={folds.has(`sphere:${g.key}`) && !g.rows.some((r) => r.session.id === current)}
          waiting={g.rows.filter((r) => isWaiting(r.session)).length}
          onFold={() => fold(`sphere:${g.key}`)}
          onNew={g.sphere ? () => setStarting(g.key) : undefined}
        >
          <ul className="flex flex-col">{foldRows(g.rows, folds, current).map(row)}</ul>
        </Group>
      ))}
      </div>
      <NewSession
        key={starting ?? ""}
        open={starting !== undefined}
        onClose={() => setStarting(undefined)}
        sphere={starting ?? null}
      />
    </nav>
  );
}
