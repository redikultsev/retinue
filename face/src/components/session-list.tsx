// The projects and their sessions (lib/spheres.ts `projectGroups`): a project is a place — its row opens its page and
// carries the amber count of what waits in it —, a sub-project indented inside it, under each its own sessions as
// Claude Code's sidebar has them: its main session first → what hangs under it (sub-sessions, forks, side questions),
// indented; a project and a session with rows under it fold, and the device remembers it. «Waiting» / «Working» / «Done» / «Pinned» are filters; «Archived», last, lists what
// was archived, with «Restore» (components/archived-list.tsx), for this visit only. A waiting session shows
// its cards under its row, with the full input and both buttons, when `cards` is set. A row opens inline (a peek):
// the session's last words and a reply box, without leaving the list. A dot marks what came since the owner looked.
import { Link, useRouterState } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { ApprovalCard } from "@/components/approval-card";
import { ArchivedList } from "@/components/archived-list";
import { Composer } from "@/components/composer";
import {
  Archive,
  ArrowUp,
  ChevronDown,
  Clock,
  Columns,
  Database,
  GitPullRequest,
  Layers,
  Pin,
  Plus,
  SphereMark,
  Star,
} from "@/components/icons";
import { NewSession } from "@/components/new-session";
import { RELATION_WORDS, RelationIcon, upOf } from "@/components/relation";
import { SessionMenu } from "@/components/session-menu";
import { Markdown } from "@/components/transcript/item";
import { ApiError, api } from "@/lib/api";
import { when } from "@/lib/format";
import { live, useLiveState } from "@/lib/live";
import { useFeed } from "@/lib/feed";
import { knownProjects, rowWaits, rowWithin, sessionProject, useProjects, within } from "@/lib/projects";
import { FILTERS, type Filter, type FoldedRow, foldProjects, foldRows, isWaiting, type ProjectGroup, projectGroups } from "@/lib/spheres";
import { approvalsOf, lastWords, listed, orderedItems, orderedSummaries, waitsOnOwner } from "@/lib/store";
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

/** The project whose page is on screen, by the address: `/spaces/travel` → `travel`, `/spaces/work/p/acme` →
 * `work/acme`; "" elsewhere. */
export function projectOfPath(pathname: string): string {
  const m = /^\/spaces\/([^/]+)(?:\/p\/(.+?))?\/?$/.exec(pathname);
  if (!m || m[1] === undefined) return "";
  const dec = (s: string) => {
    try {
      return decodeURIComponent(s);
    } catch {
      return s;
    }
  };
  return m[2] ? `${dec(m[1])}/${dec(m[2])}` : dec(m[1]);
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
  const up = upOf(s);
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
                <Star className="mr-1 inline size-3 fill-current align-[-1px] text-muted-foreground" />
                <span className="sr-only">main: </span>
              </>
            )}
            {(nested || up) && (
              // how it hangs under the row above: one icon per relation, the same as in its header and «Details»
              <span title={RELATION_WORDS[nested ? "side question" : up!.how]} className="text-muted-foreground">
                <RelationIcon how={nested ? "side question" : up!.how} className="mr-1 inline size-3 align-[-1px]" />
                <span className="sr-only">{nested ? "side question" : up!.how}: </span>
              </span>
            )}
            {s.title || s.track}
          </span>
          <span className="flex shrink-0 items-center gap-1.5 pointer-fine:group-hover/row:hidden pointer-fine:group-focus-within/row:hidden">
            {s.queued && (
              <Mini title={QUEUED}>
                <Clock />
              </Mini>
            )}
            {(s.base ?? 0) > 0 && (
              <Mini title={`Not in the Base yet: ${s.base}`}>
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
            <ChevronDown className={cn("size-3 transition-transform", folded && "-rotate-90")} />
            {kids}
            {hiddenWaiting > 0 && (
              <span aria-hidden="true" className="ml-0.5 rounded-full bg-warning px-1 text-[11px] leading-4 font-medium text-warning-foreground">
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
      {open && <Peek id={s.id} />}
    </li>
  );
}

/** A project in the list: a place — its row opens the project's page —, its mark, its name and the amber count of
 * what waits for the owner in it (its sub-projects too); a chevron folds what is under it. «Other» is a heading only. */
function ProjectHead({
  group,
  waiting,
  count,
  folded,
  onFold,
  onNew,
  on,
}: {
  group: ProjectGroup;
  waiting: number;
  /** Sessions under it, its sub-projects' too: said when it is folded. */
  count: number;
  folded: boolean;
  onFold: () => void;
  onNew?: () => void;
  /** Its page is the one on screen. */
  on: boolean;
}) {
  const inner = (
    <>
      {group.key === "" ? null : group.icon ? (
        <SphereMark icon={group.icon} className="size-3.5 shrink-0 text-muted-foreground" />
      ) : (
        <Layers className="size-3.5 shrink-0 text-muted-foreground" />
      )}
      <span className={cn("min-w-0 flex-1 truncate", group.key === "" ? "text-xs text-muted-foreground" : "font-medium text-foreground")}>{group.title}</span>
      {waiting > 0 && (
        <span title={`${waiting} waiting for you`} className="shrink-0 font-mono text-[11px] font-medium text-warning-foreground tabular-nums dark:text-warning">
          {waiting}
        </span>
      )}
    </>
  );
  const line = "flex h-[30px] min-w-0 flex-1 items-center gap-2 self-stretch pl-2 text-[13px] pointer-coarse:min-h-11 pointer-coarse:text-[15px]";
  return (
    <div
      className={cn("group/sphere flex items-center gap-0.5 rounded-md pr-1", group.depth === 0 && "mt-2", on ? "bg-accent" : group.key && "hover:bg-accent/70")}
      style={{ paddingLeft: group.depth ? `${group.depth * 0.875}rem` : undefined }}
    >
      {group.key === "" ? (
        <h2 className={line}>{inner}</h2>
      ) : (
        <h2 className="flex min-w-0 flex-1">
          <ProjectLink
            path={group.key}
            className={line}
            label={`Open the project ${group.title}${waiting ? ` · ${waiting} waiting for you` : ""}`}
            current={on}
          >
            {inner}
          </ProjectLink>
        </h2>
      )}
      {onNew && (
        <button
          type="button"
          aria-label={`New session: ${group.title}`}
          title="New session in this project"
          onClick={onNew}
          className="hit inline-flex size-6 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-accent hover:text-foreground pointer-fine:opacity-0 pointer-fine:group-hover/sphere:opacity-100 pointer-fine:focus-visible:opacity-100 [&_svg]:size-3.5"
        >
          <Plus />
        </button>
      )}
      {(count > 0 || folded) && (
        <button
          type="button"
          aria-expanded={!folded}
          aria-label={folded ? `Expand: ${group.title} (${count})` : `Collapse: ${group.title}`}
          title={folded ? `Show ${count}` : "Collapse"}
          onClick={onFold}
          className={cn(
            "hit inline-flex h-6 shrink-0 items-center justify-center gap-0.5 rounded px-1 text-[11px] tabular-nums hover:bg-accent pointer-coarse:h-11 pointer-coarse:min-w-11",
            folded ? "text-foreground" : "text-muted-foreground",
          )}
        >
          {folded && count}
          <ChevronDown className={cn("size-3 transition-transform", folded && "-rotate-90")} />
        </button>
      )}
    </div>
  );
}

/** A project's page by its folder: `/spaces/<top>`, or `/spaces/<top>/p/<the rest>` below it. */
export function ProjectLink({ path, className, label, current, children }: { path: string; className?: string; label?: string; current?: boolean; children: React.ReactNode }) {
  const [space = "", ...below] = path.split("/");
  const common = { className, "aria-label": label, title: label, "aria-current": current ? ("page" as const) : undefined };
  return below.length ? (
    <Link to="/spaces/$space/p/$" params={{ space, _splat: below.join("/") }} {...common}>
      {children}
    </Link>
  ) : (
    <Link to="/spaces/$space" params={{ space }} {...common}>
      {children}
    </Link>
  );
}

export function SessionList({ current, cards, beside, onPick, pin = api.pin, fill }: Props) {
  const state = useLiveState();
  const { sessions, approvals } = state;
  const spheres = live.meta?.spheres ?? NO_SPHERES;
  const [filter, setFilter] = useState<Filter>(savedFilter);
  const [archived, setArchived] = useState(false);
  const [starting, setStarting] = useState<string | null | undefined>(undefined); // a sphere's «＋»: its name
  const [folded, fold] = useFolded();
  const nodes = useProjects();
  const feed = useFeed();
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const known = useMemo(() => knownProjects(nodes, spheres), [nodes, spheres]);
  const groups = useMemo(
    () => projectGroups(listed(sessions), spheres, nodes ?? [], filter, Date.now() / 1000, (s) => sessionProject(s, known)),
    [sessions, spheres, nodes, filter, known],
  );
  // what waits for the owner in each project, its sub-projects' too: the feed's rows, as «Today» counts them; before
  // the feed is in, the sessions that wait
  const waitingIn = useMemo(() => {
    const rows = (feed?.groups ?? []).flatMap((g) => g.rows).filter(rowWaits);
    const all = listed(sessions).filter(isWaiting);
    return (key: string) =>
      !key ? 0 : feed ? rows.filter((r) => rowWithin(r, key, known)).length : all.filter((s) => within(sessionProject(s, known), key)).length;
  }, [feed, known, sessions]);
  const opened = projectOfPath(pathname);
  function pick(value: Filter) {
    setFilter(value);
    setArchived(false);
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
    <nav aria-label="Projects" className={cn("flex flex-col gap-1", fill && "min-h-0 flex-1 px-2")}>
      {/* what the list is: the projects, each with its sessions; the chips under it filter the sessions by state */}
      <h2 className="mx-1 shrink-0 px-1 pt-1 text-xs font-normal text-muted-foreground">Projects</h2>
      <fieldset className="mx-1 flex shrink-0 rounded-md bg-muted p-0.5">
        <legend className="sr-only">Show</legend>
        {FILTERS.map((f) => (
          <button
            key={f.value}
            type="button"
            aria-pressed={!archived && filter === f.value}
            title={f.label}
            onClick={() => pick(f.value)}
            className={cn(
              "flex h-6 min-w-0 flex-auto items-center justify-center rounded px-1.5 text-xs pointer-coarse:h-9",
              !archived && filter === f.value ? "bg-background font-medium text-foreground shadow-xs" : "text-muted-foreground hover:text-foreground",
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
        <button
          type="button"
          aria-pressed={archived}
          title="Archived"
          onClick={() => setArchived(true)}
          className={cn(
            "flex h-6 flex-none items-center justify-center rounded px-2 pointer-coarse:h-9",
            archived ? "bg-background text-foreground shadow-xs" : "text-muted-foreground hover:text-foreground",
          )}
        >
          <Archive className="size-3" />
          <span className="sr-only">Archived</span>
        </button>
      </fieldset>
      <div className={cn("flex flex-col gap-1", fill && "min-h-0 flex-1 overflow-y-auto overscroll-contain pb-4")}>
      {archived && <ArchivedList />}
      {!archived && groups.length === 0 && (
        <p className="px-3 py-8 text-center text-sm text-muted-foreground">
          {filter === "all" ? "No sessions. Start one with “+”." : "Nothing here right now."}
        </p>
      )}
      {!archived &&
        foldProjects(groups, folds, current).map((g) => {
          const under = groups.filter((x) => x.key === g.key || (g.key !== "" && x.key.startsWith(`${g.key}/`)));
          return (
            <section key={g.key || "-"} aria-label={g.title} className="flex flex-col">
              <ProjectHead
                group={g}
                waiting={waitingIn(g.key)}
                count={under.reduce((n, x) => n + x.rows.length, 0)}
                folded={g.folded}
                onFold={() => fold(`project:${g.key}`)}
                onNew={spheres.some((s) => s.name === g.key) ? () => setStarting(g.key) : undefined}
                on={opened === g.key}
              />
              {!g.folded && g.rows.length > 0 && (
                <ul className="flex flex-col" style={{ marginLeft: g.depth ? `${g.depth * 0.875}rem` : undefined }}>
                  {foldRows(g.rows, folds, current).map(row)}
                </ul>
              )}
            </section>
          );
        })}
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
