// The projects and their sessions (lib/spheres.ts `projectGroups`), as a file manager's tree: the Base's Spaces on
// top, every sub-project inside its project, folded until opened; a project is a place — its row opens its page. Under
// each its own sessions as Claude Code's sidebar has them: its main session, the pinned, the others → what hangs under
// them (sub-sessions, forks, side questions), indented; a project and a session with rows under it fold, and the
// device remembers it. No filter and no numbers: what waits for the owner is the amber dot — on its row, and on a
// folded row it is inside; the count is «Operator»'s. «Archived», the last row, opens what was archived, with
// «Restore» (components/archived-list.tsx). A waiting session shows its cards under its row, with the full input and
// both buttons, when `cards` is set. A row opens inline (a peek): the session's last words and a reply box, without
// leaving the list. A dot marks what came since the owner looked.
import { Link, useRouterState } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useState } from "react";
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
  Laptop,
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
import { type ComputerRow, computerRows, isComputer, useComputers } from "@/lib/computers";
import { when } from "@/lib/format";
import { live, useLiveState } from "@/lib/live";
import { useNearestProject, useProjectOf, useProjects } from "@/lib/projects";
import { type FoldedRow, foldKey, foldProjects, foldRows, isWaiting, type ProjectGroup, projectGroups } from "@/lib/spheres";
import { approvalsOf, lastWords, listed, orderedItems, orderedSummaries, waitsOnOwner } from "@/lib/store";
import { toast } from "@/lib/toast";
import { QUEUED, type Session, type Sphere } from "@/lib/types";
import { cn } from "@/lib/utils";

const FOLDED_KEY = "retinue-list-folded";
const NO_SPHERES: Sphere[] = [];

/** What waits for the owner, said on a row by the amber dot alone (the count is «Operator»'s). */
function WaitDot({ label }: { label: string }) {
  return <span role="img" aria-label={label} title={label} className="size-1.5 shrink-0 rounded-full bg-warning" />;
}

/** What the owner folded — session ids, and projects by `foldKey` — kept on the device. */
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

interface Props {
  /** The session open in the main panel, highlighted. */
  current?: string;
  /** Show the shown cards under «Waiting for you» (the phone's home screen). */
  cards?: boolean;
  /** Wide screens: a button that opens the session beside the current one. */
  beside?: (id: string) => void;
  onPick?: () => void;
  pin?: typeof api.pin;
  /** Fill the column it stands in: the name stays, the list under it scrolls (the wide screen's sidebar). */
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
                </Mini>
              )
            )}
            {s.waiting > 0 && (
              <span className="rounded-sm bg-warning/20 px-1 text-[11px] leading-4 font-medium text-warning-foreground dark:text-warning">waiting</span>
            )}
            {s.pinned && <Pin className="size-3 fill-current text-muted-foreground pointer-coarse:hidden" />}
            {s.unread && <span role="img" aria-label="unread" title="Something new" className="size-1.5 shrink-0 rounded-full bg-primary" />}
          </span>
        </Link>
        {kids > 0 && onFold && (
          <button
            type="button"
            aria-expanded={!folded}
            aria-label={folded ? `Expand: ${s.title}${hiddenWaiting ? " · something waits for you" : ""}` : `Collapse nested: ${s.title}`}
            title={`${folded ? "Show" : "Collapse"} nested${s.children ? " · sub-sessions open" : ""}`}
            onClick={onFold}
            className={cn(
              "hit inline-flex h-6 shrink-0 items-center justify-center gap-0.5 rounded px-1 text-[11px] tabular-nums hover:bg-accent pointer-coarse:h-11 pointer-coarse:min-w-11",
              folded ? "text-foreground" : "text-muted-foreground",
            )}
          >
            <ChevronDown className={cn("size-3 transition-transform", folded && "-rotate-90")} />
            {hiddenWaiting > 0 && <span aria-hidden="true" className="size-1.5 rounded-full bg-warning" />}
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

/** A session of one of the owner's computers (lib/computers.ts): its state, its title, the computer's mark and when it
 * was last active. It opens nothing here; on the computer, its link opens the session there. An off computer's row is
 * greyed, with when the computer was last seen. */
function ComputerItem({ s, depth }: { s: ComputerRow; depth: number }) {
  const { device, off, seen, folder, link } = s.computer;
  const said = off ? `${device} off · seen ${when(seen)}` : `on ${device} · ${when(s.at)}`;
  const inner = (
    <>
      <StateDot s={s} />
      <span className={cn("min-w-0 flex-1 truncate text-[13px] pointer-coarse:text-[15px]", off ? "text-muted-foreground" : "text-foreground/85")}>
        {s.title || "Untitled"}
      </span>
      <span className="flex shrink-0 items-center gap-1 text-[11px] text-muted-foreground tabular-nums">
        {s.state === "waiting" && (
          <span className="rounded-sm bg-warning/20 px-1 leading-4 font-medium text-warning-foreground dark:text-warning">waiting</span>
        )}
        <Laptop className="size-3" />
        <span className="sr-only">{said}</span>
        <span aria-hidden="true">{off ? "off" : when(s.at)}</span>
      </span>
    </>
  );
  const line = "flex h-[30px] min-w-0 flex-1 items-center gap-2 self-stretch rounded-md pr-1 pl-2 pointer-coarse:h-auto pointer-coarse:min-h-11";
  const title = `${s.title || "Untitled"} · ${said}${folder ? ` · ${folder}` : ""}`;
  return (
    <li className="flex flex-col" style={{ marginLeft: depth ? `${depth * 1.25}rem` : undefined }}>
      {link ? (
        <a href={link} title={title} className={cn(line, "hover:bg-accent/70")}>
          {inner}
        </a>
      ) : (
        <div title={title} className={line}>
          {inner}
        </div>
      )}
    </li>
  );
}

/** A project in the list: a place — its row opens the project's page —, its mark and its name; folded, the amber dot
 * while something under it waits for the owner. A chevron folds what is under it. «Other» is a heading only. */
function ProjectHead({
  group,
  waiting,
  fills,
  folded,
  onFold,
  onNew,
  on,
}: {
  group: ProjectGroup;
  /** Something under it waits for the owner: said on its row while it is folded. */
  waiting: boolean;
  /** Something is under it — sessions or sub-projects —, so it folds. */
  fills: boolean;
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
      <span className={cn("min-w-0 flex-1 truncate", group.key === "" ? "text-xs text-muted-foreground" : group.depth ? "text-foreground/85" : "font-medium text-foreground")}>
        {group.title}
      </span>
      {waiting && folded && <WaitDot label="Something waits for you in it" />}
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
            label={`Open the project ${group.title}`}
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
      {fills && (
        <button
          type="button"
          aria-expanded={!folded}
          aria-label={folded ? `Expand: ${group.title}` : `Collapse: ${group.title}`}
          title={folded ? "Show" : "Collapse"}
          onClick={onFold}
          className={cn(
            "hit inline-flex h-6 shrink-0 items-center justify-center gap-0.5 rounded px-1 text-[11px] tabular-nums hover:bg-accent pointer-coarse:h-11 pointer-coarse:min-w-11",
            folded ? "text-foreground" : "text-muted-foreground",
          )}
        >
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
  const [archived, setArchived] = useState(false);
  const [starting, setStarting] = useState<string | null | undefined>(undefined); // a sphere's «＋»: its name
  const [folded, fold] = useFolded();
  const nodes = useProjects();
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const of = useProjectOf(spheres);
  const nearestOf = useNearestProject(spheres);
  const machines = useComputers();
  // a computer's session goes under the nearest project of the Base folder its computer gave, else «Other»
  const where = useCallback((s: Session) => (isComputer(s) ? nearestOf(s.project) : of(s)), [of, nearestOf]);
  const groups = useMemo(
    () => projectGroups([...listed(sessions), ...computerRows(machines)], spheres, nodes ?? [], Date.now() / 1000, where),
    [sessions, machines, spheres, nodes, where],
  );
  const opened = projectOfPath(pathname);
  const folds = folded;
  const row = ({ session: s, depth, side, kids, folded: shut, waiting }: FoldedRow) =>
    isComputer(s) ? (
      <ComputerItem key={s.id} s={s} depth={depth} />
    ) : (
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
      {/* what the list is: the projects, each with its sessions */}
      <h2 className="mx-1 shrink-0 px-1 pt-1 text-xs font-normal text-muted-foreground">Projects</h2>
      <div className={cn("flex flex-col gap-1", fill && "min-h-0 flex-1 overflow-y-auto overscroll-contain pb-4")}>
      {groups.length === 0 && <p className="px-3 py-8 text-center text-sm text-muted-foreground">No sessions. Start one with “+”.</p>}
      {foldProjects(groups, folds, current).map((g) => {
          const under = groups.filter((x) => x.key === g.key || (g.key !== "" && x.key.startsWith(`${g.key}/`)));
          return (
            <section key={g.key || "-"} aria-label={g.title} className="flex flex-col">
              <ProjectHead
                group={g}
                waiting={under.some((x) => x.rows.some((r) => isWaiting(r.session)))}
                fills={g.rows.length > 0 || under.length > 1}
                folded={g.folded}
                onFold={() => fold(foldKey(g))}
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
      <button
        type="button"
        aria-expanded={archived}
        onClick={() => setArchived(!archived)}
        className="mt-2 flex h-[30px] shrink-0 items-center gap-2 rounded-md pr-1 pl-2 text-left text-[13px] text-muted-foreground hover:bg-accent/70 hover:text-foreground pointer-coarse:min-h-11 pointer-coarse:text-[15px]"
      >
        <Archive className="size-3.5 shrink-0" />
        <span className="min-w-0 flex-1 truncate">Archived</span>
        <ChevronDown className={cn("size-3 transition-transform", !archived && "-rotate-90")} />
      </button>
      {archived && <ArchivedList />}
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
