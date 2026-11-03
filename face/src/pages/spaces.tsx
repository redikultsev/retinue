// «Spaces»: the Base in the app, read only (retinue/spaces.py). `/spaces` — the grid of Spaces with what waits
// in each; `/spaces/<space>` — a project as a screen (docs/face.md «A project»): «Overview» — its passport, what is
// the owner's in it, its sessions and sub-projects —, its collections' views, «Records», «Files» and «Operator»;
// `/spaces/<space>/p/<path>` — a project below it, the same screen; `/spaces/<space>/r/<path>` — one Record to read,
// its head in plain words. «Discuss», «Plan» and «Edit with a session» start a session in the project's folder, in
// the Space's sphere, with the Record, the question, the task or the passport in its first message: the Base changes
// only through a session.
import { PageHeader, PAGE_X } from "@/components/page-header";
import { Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { BaseMarkdown } from "@/components/base-markdown";
import { FileRow } from "@/components/files";
import { MapView } from "@/components/map";
import { Operator } from "@/components/operator";
import { RELATION_WORDS, RelationIcon, upOf } from "@/components/relation";
import { Agenda } from "@/components/views/agenda";
import { Board } from "@/components/views/board";
import { SpaceChanges } from "@/components/views/changes";
import { Chart } from "@/components/views/chart";
import { AddButton, Editor, type Move } from "@/components/views/edit";
import { Figure, isFigure } from "@/components/views/figures";
import { SpaceProposals } from "@/components/views/proposals";
import { Queue } from "@/components/views/queue";
import { StatList, StatStrip, statsWindow } from "@/components/views/stat";
import { Strip } from "@/components/views/strip";
import { ArrowLeft, ChevronRight, Circle, Database, Layers, MessageCircle, Pencil, Plus, Question, SphereMark } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api } from "@/lib/api";
import { collectionsApi, layout, type Placed, type Places, placedCount, type SpaceViews, useSpaceViews } from "@/lib/collections";
import { type EditApi, editApi, type ProposalsApi, proposalsApi } from "@/lib/edit";
import { plural, sessionStateWord, when } from "@/lib/format";
import { useFeed } from "@/lib/feed";
import { live, useLiveState } from "@/lib/live";
import { learnProjects, useProjectOf, within } from "@/lib/projects";
import { listed, RECENT_S } from "@/lib/store";
import {
  baseFile,
  baseView,
  discussText,
  headWords,
  NATURE_WORDS,
  NATURES,
  orderSpaces,
  ownTitle,
  type Passport,
  type ProjectFile,
  type ProjectRow,
  type RecordView,
  rowWords,
  type ShowcaseItem,
  type SpaceRow,
  type SpaceView,
  sessionBadges,
  spacesApi,
  TYPE_WORDS,
} from "@/lib/spaces";
import { toast } from "@/lib/toast";
import type { Session, Sphere } from "@/lib/types";
import { cn, inputId, LIST, LIST_ROW } from "@/lib/utils";
import { waitingIn } from "@/lib/waiting";

const NO_SPHERES: Sphere[] = [];

// --- loading -------------------------------------------------------------------------------------------------------

// What was read last, by address: going back shows it at once while it is read again.
const seen = new Map<string, unknown>();

function useRead<T>(key: string, read: () => Promise<T>): { data: T | undefined; error: string | null } {
  const [data, setData] = useState<T | undefined>(() => seen.get(key) as T | undefined);
  const [error, setError] = useState<string | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: `read` is new each render; the key names what it reads
  useEffect(() => {
    let gone = false;
    setData(seen.get(key) as T | undefined);
    setError(null);
    read()
      .then((found) => {
        if (gone) return;
        seen.set(key, found);
        setData(found);
      })
      .catch((e) => {
        if (!gone) setError(e instanceof ApiError ? e.message : "Did not load.");
      });
    return () => {
      gone = true;
    };
  }, [key]);
  return { data, error };
}

/** For tests: forget what was read, and the tab each Space was left on. */
export function forgetRead() {
  seen.clear();
  tabOf.clear();
}

function useSpheres(): Sphere[] {
  useLiveState(); // meta arrives with the first snapshot
  return live.meta?.spheres ?? NO_SPHERES;
}

// --- pieces ----------------------------------------------------------------------------------------------------------

function Header({
  up,
  title,
  width,
  children,
}: { up?: { space?: string; label: string }; title: string; width?: string; children?: React.ReactNode }) {
  const back = "hit -ml-2 inline-flex size-8 shrink-0 items-center justify-center rounded-md hover:bg-accent";
  return (
    <PageHeader
      title={title}
      width={width}
      actions={children}
      back={
        up?.space ? (
          <Link to="/spaces/$space" params={{ space: up.space }} aria-label={up.label} title={up.label} className={back}>
            <ArrowLeft className="size-5" />
          </Link>
        ) : up ? (
          <Link to="/spaces" aria-label={up.label} title={up.label} className={back}>
            <ArrowLeft className="size-5" />
          </Link>
        ) : null
      }
    />
  );
}

function Section({ title, count, children, action }: { title: string; count?: number; children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-1.5">
      <div className="flex min-h-8 items-center gap-2 px-1">
        <h2 className="text-[13px] font-medium">{title}</h2>
        {count !== undefined && <span className="text-[13px] text-muted-foreground tabular-nums">{count}</span>}
        <span className="flex-1" />
        {action}
      </div>
      {children}
    </section>
  );
}

const Empty = ({ children }: { children: React.ReactNode }) => <p className="px-1 py-2 text-sm text-muted-foreground">{children}</p>;

const sphereOf = (spheres: Sphere[], name: string) => spheres.find((s) => s.name === name);

function spaceTitle(name: string, sphere: Sphere | undefined, showcase?: string) {
  return sphere?.title || showcase || name;
}

function Badges({ waiting, working, questions }: { waiting: number; working: number; questions: number }) {
  return (
    <span className="flex flex-wrap gap-1">
      {waiting > 0 && <Badge variant="warning">{`${waiting} waiting`}</Badge>}
      {working > 0 && <Badge variant="secondary">{`${working} working`}</Badge>}
      {questions > 0 && <Badge variant="outline">{`${questions} ${plural(questions, "question", "questions")}`}</Badge>}
    </span>
  );
}

// --- «Discuss» -------------------------------------------------------------------------------------------------------

type Topic = { record?: { path: string; title: string }; question?: string; task?: string; passport?: boolean };

/** The dialog's title and what its first message is about, by its topic. */
function topicWords(topic: Topic | null): { title: string; what: string } {
  if (topic?.record) return { title: "Discuss: record", what: "Record" };
  if (topic?.question) return { title: "Discuss: open question", what: "Open question" };
  if (topic?.task) return { title: "Plan a task", what: "Task" };
  if (topic?.passport) return { title: "Edit the passport", what: "Passport" };
  return { title: "New session", what: "" };
}

/** A new session in the project's folder (`project`, the Space itself by default) and the Space's sphere (its track),
 * its first message naming the Record or quoting the question; the owner adds his own words. A Space no sphere names:
 * the owner picks the track. */
export function Discuss({
  space,
  project = space,
  title,
  topic,
  onClose,
  start = api.newSession,
}: {
  space: string;
  project?: string;
  title?: string;
  topic: Topic | null;
  onClose: () => void;
  start?: typeof api.newSession;
}) {
  const spheres = useSpheres();
  const sphere = sphereOf(spheres, space);
  const tracks = live.meta?.tracks ?? [];
  const [track, setTrack] = useState(sphere?.track ?? tracks[0]?.name ?? "");
  const [own, setOwn] = useState("");
  const [busy, setBusy] = useState(false);
  const [id, setId] = useState(inputId);
  const navigate = useNavigate();
  const text = topic ? discussText(topic.record ? space : project, topic, own) : "";
  const chosen = sphere?.track ?? track;

  async function submit() {
    if (!chosen || !text.trim()) return;
    setBusy(true);
    try {
      const res = await start(chosen, text, id, sphere?.name ?? null, [], project);
      setOwn("");
      setId(inputId());
      onClose();
      await navigate({ to: "/s/$id", params: { id: res.session } });
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "The session did not start.", "error");
    } finally {
      setBusy(false);
    }
  }

  const { title: heading, what } = topicWords(topic);
  return (
    <Dialog open={topic !== null} onClose={onClose} title={heading}>
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <p className="text-sm text-muted-foreground">
          New session in {project === space ? "the Space" : "the project"} “{title ?? spaceTitle(space, sphere)}”{what ? ", the first message will be:" : "."}
        </p>
        {what && (
          <blockquote data-testid="discuss-head" className="max-h-40 overflow-y-auto rounded-md border bg-muted/40 p-2 text-sm whitespace-pre-wrap">
            {discussText(topic?.record ? space : project, topic ?? {}, "")}
          </blockquote>
        )}
        {!sphere && tracks.length > 1 && (
          <fieldset className="flex flex-wrap gap-2">
            <legend className="mb-1 text-sm font-medium">Track</legend>
            {tracks.map((t) => (
              <label key={t.name} className="flex min-h-11 cursor-pointer items-center gap-2 rounded-md border px-3 has-[:checked]:border-primary has-[:checked]:bg-accent">
                <input type="radio" name="track" checked={chosen === t.name} onChange={() => setTrack(t.name)} className="accent-primary" />
                <span className="text-sm">{t.name}</span>
              </label>
            ))}
          </fieldset>
        )}
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium">{topic?.passport ? "What should change?" : what ? "What should I ask or do?" : "What should I do?"}</span>
          <Textarea value={own} onChange={(e) => setOwn(e.target.value)} rows={3} placeholder={what ? "Can be empty" : ""} />
        </label>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || !chosen || !text.trim()}>
            {busy ? "Starting…" : "Start"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

// --- the grid ----------------------------------------------------------------------------------------------------------

export function SpacesPage({ read = spacesApi.list }: { read?: typeof spacesApi.list }) {
  const { sessions } = useLiveState();
  const spheres = useSpheres();
  const { data, error } = useRead("spaces", read);
  const live_ = useMemo(() => sessionBadges(listed(sessions)), [sessions]);
  const loaded = Object.keys(sessions).length > 0;
  const rows = useMemo(() => orderSpaces(data?.spaces ?? [], spheres), [data, spheres]);
  const badges = (row: SpaceRow) => (loaded ? (live_.get(row.name) ?? { waiting: 0, working: 0 }) : row);
  return (
    <div className="flex flex-col">
      <Header title="Spaces" />
      <main className={cn("mx-auto w-full max-w-3xl pt-4 pb-[max(1rem,env(safe-area-inset-bottom))]", PAGE_X)}>
        {error && !data && <Empty>{error}</Empty>}
        {!error && !data && <Empty>Loading…</Empty>}
        {data && rows.length === 0 && <Empty>The Base has no Spaces.</Empty>}
        <ul className="grid grid-cols-2 gap-2 sm:grid-cols-3">
          {rows.map((row) => {
            const sphere = sphereOf(spheres, row.name);
            const { waiting, working } = badges(row);
            return (
              <li key={row.name}>
                <ToProject path={row.name} className="flex h-full min-h-24 flex-col gap-2 rounded-xl border bg-card p-3 hover:bg-accent">
                  <span className="flex items-center gap-2">
                    {sphere?.icon ? <SphereMark icon={sphere.icon} className="size-4 shrink-0 text-muted-foreground" /> : <Layers className="size-4 shrink-0 text-muted-foreground" />}
                    <span className="min-w-0 flex-1 truncate font-medium">{spaceTitle(row.name, sphere, row.title)}</span>
                  </span>
                  <Badges waiting={waiting} working={working} questions={row.questions} />
                </ToProject>
              </li>
            );
          })}
        </ul>
      </main>
    </div>
  );
}

// --- the pieces of a project's screen ------------------------------------------------------------------------

function Lines({ items, onDiscuss }: { items: ShowcaseItem[]; onDiscuss?: (item: ShowcaseItem) => void }) {
  return (
    // one surface with dividers, as every list of the app (`LIST`); a line of prose between items stays plain text
    <ul className={items.some((item) => item.list) ? LIST : "flex flex-col gap-1"}>
      {items.map((item, i) => (
        <li key={i} className={cn("group/row flex items-start gap-2 px-3 py-2", !item.list && "text-muted-foreground")}>
          <BaseMarkdown text={item.text} className="min-w-0 flex-1" />
          {item.list && onDiscuss && (
            <Button
              size="sm"
              variant="ghost"
              className="hit -my-1 h-8 shrink-0 text-muted-foreground group-hover/row:text-foreground md:h-8"
              onClick={() => onDiscuss(item)}
              aria-label="Discuss the question"
            >
              <MessageCircle />
              <span className="hidden sm:inline">Discuss</span>
            </Button>
          )}
        </li>
      ))}
    </ul>
  );
}

/** A board or a queue in its own section; one the platform could not make says why in its place. */
function Shown({ placed }: { placed: Placed }) {
  const { collection, view } = placed;
  if (view.error === undefined && view.kind === "chart") return <Chart collection={collection} view={view} />; // titled inside
  if (isFigure(view)) return <Figure collection={collection} view={view} />; // sums by month, a year's line, a view in code
  const add = view.error === undefined && (view.kind === "queue" || view.kind === "board") ? view.add : null;
  return (
    <Section
      title={view.title}
      count={view.error === undefined && view.kind === "queue" ? view.count : undefined}
      action={add ? <AddButton collection={collection.name} spec={add} /> : undefined}
    >
      {view.error !== undefined ? (
        <Empty>{`Not shown: ${view.error}`}</Empty>
      ) : view.kind === "queue" ? (
        <Queue collection={collection} view={view} />
      ) : view.kind === "board" ? (
        <Board collection={collection} view={view} />
      ) : null}
    </Section>
  );
}

// --- one Space's tabs --------------------------------------------------------------------------------------------------

/** The first tab: the boards as strips over the work, the queues down the middle, and — when the Space has them — the
 * days ahead and the numbers in a narrow column at the right, which stays in view; on a phone it comes after. */
function FirstTab({ places }: { places: Places }) {
  const side = places.agendas.length > 0 || places.stats.length > 0;
  const numbers = statsWindow(places.stats);
  const ahead = agendaDays(places.agendas);
  return (
    <div className="flex flex-col gap-3">
      {places.boards.map(({ collection, view }) =>
        view.error === undefined && view.kind === "board" ? (
          <Strip key={`${collection.name}/${view.name}`} collection={collection} view={view} />
        ) : (
          <Shown key={`${collection.name}/${view.name}`} placed={{ collection, view }} />
        ),
      )}
      <div className={cn("grid gap-x-8 gap-y-5", side && "lg:grid-cols-[minmax(0,1fr)_20rem]")}>
        <div className="flex min-w-0 flex-col gap-5">
          {places.queues.map((p) => (
            <Shown key={`${p.collection.name}/${p.view.name}`} placed={p} />
          ))}
          {places.charts.map((p) => (
            <Shown key={`${p.collection.name}/${p.view.name}`} placed={p} />
          ))}
        </div>
        {side && (
          <aside className="flex flex-col gap-5 lg:sticky lg:top-16 lg:self-start">
            {places.agendas.length > 0 && (
              <Section title={ahead ? `Next ${ahead} days` : "Days ahead"}>
                <Agenda agendas={places.agendas} />
              </Section>
            )}
            {places.stats.length > 0 && (
              <Section title={numbers.title} action={<span className="text-xs text-muted-foreground">{numbers.days}</span>}>
                <StatList stats={places.stats} />
              </Section>
            )}
          </aside>
        )}
      </div>
    </div>
  );
}

/** Another tab the views name: its numbers as tiles, its charts, then its boards and queues whole, its days ahead. */
function ViewsTab({ places }: { places: Places }) {
  return (
    <div className="flex flex-col gap-4">
      {places.stats.length > 0 && <StatStrip stats={places.stats} />}
      {[...places.charts, ...places.boards, ...places.queues].map((p) => (
        <Shown key={`${p.collection.name}/${p.view.name}`} placed={p} />
      ))}
      {places.agendas.length > 0 && (
        <Section title="Days ahead">
          <Agenda agendas={places.agendas} />
        </Section>
      )}
    </div>
  );
}

/** Whether a tab holds only queues, every one of them empty now. */
const idle = (p: Places) =>
  p.queues.length > 0 &&
  p.stats.length + p.boards.length + p.agendas.length + p.charts.length === 0 &&
  p.queues.every(({ view }) => view.error === undefined && view.kind === "queue" && view.count === 0);

/** How many days the agendas span: the «Next 7 days» over them. */
function agendaDays(agendas: Placed[]): number {
  const views = agendas.flatMap(({ view }) => (view.error === undefined && view.kind === "agenda" ? [view] : []));
  if (!views.length) return 0;
  const first = views.map((v) => v.first).sort()[0]!;
  const last = views.map((v) => v.last).sort().at(-1)!;
  return Math.round((Date.parse(last) - Date.parse(first)) / 86_400_000) + 1;
}

/** The rows of a tab's queues that are the owner's to act on: the count its tab shows. */
const actsOf = (places: Places) =>
  places.queues.reduce((n, { view }) => n + (view.error === undefined && view.kind === "queue" && view.mark !== "wait" ? view.count : 0), 0);

/** The column a record's sheet offers to move it to, by the first board of its collection. */
export function movesOf(answer: SpaceViews | null | undefined): Record<string, Move> {
  const moves: Record<string, Move> = {};
  for (const collection of answer?.collections ?? [])
    for (const view of collection.views)
      if (view.error === undefined && view.kind === "board" && !moves[collection.name])
        moves[collection.name] = { field: view.group, values: view.columns.map((c) => c.value) };
  return moves;
}

// --- one project: a Space or a project below it --------------------------------------------------------------------

/** The waiting dot of a session's row: amber — it waits for the owner; blue, breathing — at work; none — quiet. */
function StateDot({ s }: { s: Session }) {
  const waits = s.state === "waiting" || s.waiting > 0;
  return (
    <span
      aria-hidden
      className={cn("size-2 shrink-0 rounded-full", waits ? "bg-warning" : s.state === "active" ? "animate-pulse bg-primary" : "bg-transparent")}
    />
  );
}

/** A session in the project's «Sessions»: its state, its title (how it hangs under another, by its icon), its state
 * in words and when, a chevron. */
function SessionRow({ s }: { s: Session }) {
  const waits = s.state === "waiting" || s.waiting > 0;
  const up = upOf(s);
  return (
    <li>
      <Link to="/s/$id" params={{ id: s.id }} className={cn(LIST_ROW, "min-h-11 items-center gap-3")}>
        <StateDot s={s} />
        <span className="min-w-0 flex-1">
          <span className={cn("block truncate text-sm", s.unread && "font-medium")}>
            {up && (
              <span title={RELATION_WORDS[up.how]} className="text-muted-foreground">
                <RelationIcon how={up.how} className="mr-1 inline size-3 align-[-1px]" />
                <span className="sr-only">{up.how}: </span>
              </span>
            )}
            {s.title || s.track}
          </span>
          <span className="block truncate text-xs text-muted-foreground">
            {waits ? "waiting for you" : sessionStateWord(s.state)} · {when(s.at)}
          </span>
        </span>
        {s.waiting > 0 && <Badge variant="warning">{s.waiting}</Badge>}
        <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
      </Link>
    </li>
  );
}

/** A sub-project in the project's «Sessions»: a place to open, with what waits in it. */
function SubProjectRow({ row, waiting, sessions }: { row: ProjectRow; waiting: number; sessions: number }) {
  const words = [
    "sub-project",
    sessions ? `${sessions} ${plural(sessions, "session", "sessions")}` : "",
    row.questions ? `${row.questions} ${plural(row.questions, "question", "questions")}` : "",
  ].filter(Boolean);
  return (
    <li>
      <ToProject path={row.name} className={cn(LIST_ROW, "min-h-11 items-center gap-3")}>
        <Layers className="size-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm">{row.title}</span>
          <span className="block truncate text-xs text-muted-foreground">{words.join(" · ")}</span>
        </span>
        {waiting > 0 && (
          <span className="shrink-0 font-mono text-xs font-medium text-warning-foreground tabular-nums dark:text-warning" title={`${waiting} waiting for you`}>
            {waiting}
          </span>
        )}
        <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
      </ToProject>
    </li>
  );
}

/** The rules' first lines, joined: what the passport's «Rules» row says before «All rules» opens them whole. */
export function rulesGist(rules: string, most = 3): { gist: string; more: boolean } {
  const lines = rules.split("\n").map((l) => l.trim()).filter((l) => l && !/^#{1,6}\s/.test(l));
  const items = lines.filter((l) => /^([-*+]|\d{1,3}[.)])\s+/.test(l)).map((l) => l.replace(/^([-*+]|\d{1,3}[.)])\s+/, "").replace(/[.;]$/, ""));
  const pool = items.length ? items : lines;
  // more than the gist says: more lines than it shows, or prose beside its list
  const more = pool.length > most || (items.length > 0 && lines.length > items.length);
  return { gist: pool.slice(0, most).join(" · "), more };
}

/** A project's passport at the top of its page: why it is (16 px), what is in its scope and what is not, its rules —
 * a table of two columns —, and «Edit with a session». No passport yet: said, with the same way to write one. */
function PassportCard({ passport, rules, onEdit }: { passport: Passport | null | undefined; rules: string; onEdit: () => void }) {
  const { gist, more } = rulesGist(rules);
  const row = "contents";
  const label = "text-[13px] leading-5 text-muted-foreground";
  const cell = "min-w-0 text-[13px] leading-5 [&_p]:m-0";
  return (
    <section aria-label="Passport" className="flex flex-col gap-3 rounded-xl border bg-card px-4 py-4 md:px-5">
      {passport?.why ? (
        <BaseMarkdown text={passport.why} className="max-w-[72ch] text-base leading-6 text-pretty md:text-base [&_p]:m-0" />
      ) : (
        <p className="text-sm text-muted-foreground">No passport yet: the project's AGENTS.md has no «Why» section.</p>
      )}
      {(passport?.in_scope || passport?.not_in_scope || rules) && (
        <dl className="grid max-w-[60rem] grid-cols-[6.5rem_minmax(0,1fr)] gap-x-4 gap-y-1.5 max-sm:grid-cols-1 max-sm:gap-y-0.5">
          {passport?.in_scope && (
            <div className={row}>
              <dt className={label}>In scope</dt>
              <dd className={cn(cell, "max-sm:mb-1.5")}>
                <BaseMarkdown text={passport.in_scope} className="text-[13px] leading-5 md:text-[13px]" />
              </dd>
            </div>
          )}
          {passport?.not_in_scope && (
            <div className={row}>
              <dt className={label}>Not in scope</dt>
              <dd className={cn(cell, "max-sm:mb-1.5")}>
                <BaseMarkdown text={passport.not_in_scope} className="text-[13px] leading-5 md:text-[13px]" />
              </dd>
            </div>
          )}
          {rules && (
            <div className={row}>
              <dt className={label}>Rules</dt>
              <dd className={cell}>
                {gist && <BaseMarkdown text={gist} className="text-[13px] leading-5 md:text-[13px]" />}
                {(more || !gist) && (
                  <details className="group/rules mt-1">
                    <summary className="w-fit cursor-pointer text-xs text-muted-foreground hover:text-foreground">All rules</summary>
                    <BaseMarkdown text={rules} className="pt-2 text-[13px] md:text-[13px]" />
                  </details>
                )}
              </dd>
            </div>
          )}
        </dl>
      )}
      <div>
        <Button variant="outline" size="sm" onClick={onEdit} className="max-md:h-11">
          <Pencil />
          {passport ? "Edit with a session" : "Write it with a session"}
        </Button>
      </div>
    </section>
  );
}

/** «Yours»: what in the project is the owner's — its Open questions (each with «Discuss») and its Tasks (each with
 * «Plan») — one list; a line of prose between them stays as it is. */
function Yours({ questions, tasks, onTopic }: { questions: ShowcaseItem[]; tasks: ShowcaseItem[]; onTopic: (t: Topic) => void }) {
  const rows = [...questions.map((item) => ({ item, kind: "question" as const })), ...tasks.map((item) => ({ item, kind: "task" as const }))].filter(
    (r) => r.item.list,
  );
  if (!rows.length) return null;
  return (
    <Section title="Yours" count={rows.length}>
      <ul className={LIST} aria-label="Yours">
        {rows.map(({ item, kind }, i) => (
          <li key={`${kind}:${i}`} className="flex min-h-11 items-center gap-3 px-3 py-2">
            {kind === "question" ? (
              <Question aria-label="Open question" className="size-4 shrink-0 text-muted-foreground" />
            ) : (
              <Circle aria-label="Task" className="size-4 shrink-0 text-muted-foreground" />
            )}
            <BaseMarkdown text={item.text} className="min-w-0 flex-1 text-sm md:text-sm [&_p]:m-0" />
            <Button
              size="sm"
              variant="outline"
              className="shrink-0 max-md:h-11"
              onClick={() => onTopic(kind === "question" ? { question: item.text } : { task: item.text })}
              aria-label={kind === "question" ? "Discuss the question" : "Plan the task"}
            >
              {kind === "question" ? "Discuss" : "Plan"}
            </Button>
          </li>
        ))}
      </ul>
    </Section>
  );
}

/** «Records»: every Record of the project in one list — its nature and what its head says on the second line —, with
 * a search once there are more than a few. */
function RecordsList({ space, below, view }: { space: string; below: string; view: SpaceView }) {
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const all = NATURES.flatMap((nature) => view.records[nature].map((row) => ({ nature, row })));
  const shown = all.filter(({ row }) => !q || row.title.toLowerCase().includes(q) || row.path.toLowerCase().includes(q));
  const summary = NATURES.filter((n) => view.records[n].length)
    .map((n) => `${NATURE_WORDS[n]} ${view.records[n].length}`)
    .join(" · ");
  return (
    <Section title="Records" count={all.length} action={summary ? <span className="text-xs text-muted-foreground">{summary}</span> : undefined}>
      {all.length === 0 && <Empty>No Records.</Empty>}
      {all.length > 6 && (
        <Input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Find a Record" aria-label="Find a Record" className="mb-1" />
      )}
      {q && shown.length === 0 && <Empty>Nothing found.</Empty>}
      {shown.length > 0 && (
        <ul className={LIST} aria-label="Records">
          {shown.map(({ nature, row }) => {
            const words = rowWords(row, nature);
            return (
              <li key={`${nature}:${row.path}`}>
                <Link to="/spaces/$space/r/$" params={{ space, _splat: below ? `${below}/${row.path}` : row.path }} className={cn(LIST_ROW, "min-h-11 items-center gap-3")}>
                  <Database className="size-4 shrink-0 text-muted-foreground" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm">{row.title}</span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {NATURE_WORDS[nature]}
                      {words.map((w) => (
                        <span key={w.key} className={cn(w.warn && "text-warning-foreground dark:text-warning")}>
                          {" · "}
                          {w.text}
                        </span>
                      ))}
                      {row.big && " · large — on a computer"}
                    </span>
                  </span>
                  <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </Section>
  );
}

type OperatorView = "cards" | "map";
const OPERATOR_VIEW_KEY = "retinue-project-operator";

function keptOperatorView(): OperatorView {
  try {
    return localStorage.getItem(OPERATOR_VIEW_KEY) === "map" ? "map" : "cards";
  } catch {
    return "cards";
  }
}

function keepOperatorView(view: OperatorView): void {
  try {
    localStorage.setItem(OPERATOR_VIEW_KEY, view);
  } catch {
    // storage unavailable: the pick holds while the page is open
  }
}

/** A project's «Operator» as cards or as the map of its sessions in their places — the switch «Today» has. */
function OperatorSwitch({ view, onView }: { view: OperatorView; onView: (v: OperatorView) => void }) {
  return (
    <fieldset aria-label="Show" className="inline-flex h-9 w-fit items-center self-start rounded-md border p-0.5 max-md:h-11">
      {(["cards", "map"] as const).map((v) => (
        <button
          key={v}
          type="button"
          aria-pressed={view === v}
          onClick={() => onView(v)}
          className={cn("h-7 rounded px-3 text-[13px] max-md:h-9 whitespace-nowrap transition-colors", view === v ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground")}
        >
          {v === "cards" ? "Cards" : "Map"}
        </button>
      ))}
    </fieldset>
  );
}

const OVERVIEW = "Overview";
const RECORDS = "Records";
const FILES = "Files";
const OPERATOR = "Operator";

/** The tab a project was left on, kept while the app is open. */
const tabOf = new Map<string, string>();

const sessionWaits = (s: Session) => s.state === "waiting" || s.waiting > 0;

/** One project — a Space (`space`, with its collections' views) or a project below it — as a screen: a header of one
 * line (its mark, its name, «project · N sub-projects», its tabs), and the tabs: «Overview» — its passport, what is
 * the owner's in it, its sessions and sub-projects —, the views its collections name, «Records», «Files» and
 * «Operator»: the sessions' requests of this project and its sub-projects, as on «Today». */
function ProjectScreen({
  path,
  data,
  error,
  ids,
  views,
  edit,
  proposals,
}: {
  path: string;
  data: SpaceView | undefined;
  error: string | null;
  /** The sessions the platform says were started in its folder. */
  ids: string[];
  /** A Space's collections; absent below a Space. */
  views?: { shown: SpaceViews | null | undefined; error: string | null; tabs: ReturnType<typeof layout> };
  edit?: EditApi;
  proposals?: ProposalsApi;
}) {
  const { sessions } = useLiveState();
  const spheres = useSpheres();
  const feed = useFeed();
  const now = useNowS();
  const subs = data?.projects ?? [];
  // this project and its sub-projects are known to every screen's matching, the list read or not
  const learned = [path, ...subs.map((p) => p.name)].join("\n");
  useEffect(() => learnProjects(learned.split("\n")), [learned]);
  const of = useProjectOf(spheres);
  const [top = "", ...rest] = path.split("/");
  const below = rest.join("/");
  const sphere = below ? undefined : sphereOf(spheres, top);
  const [topic, setTopic] = useState<Topic | null>(null);
  const [picked, setPicked] = useState<string>(() => tabOf.get(path) ?? OVERVIEW);
  const [operatorView, setOperatorView] = useState<OperatorView>(keptOperatorView);
  const pickOperatorView = (v: OperatorView) => {
    keepOperatorView(v);
    setOperatorView(v);
  };
  const title = sphere?.title || data?.title || path.split("/").pop() || path;

  // its own sessions: started in its folder, or of its sphere with no folder of their own; not a side question
  const mine = useMemo(() => {
    const at = Date.now() / 1000;
    const rank = (s: Session) => (sessionWaits(s) ? 0 : s.state === "active" ? 1 : 2);
    return listed(sessions)
      .filter((s) => !(s.side ?? !!s.parent))
      .filter((s) => s.state !== "closed" || sessionWaits(s) || at - s.at < RECENT_S)
      .filter((s) => ids.includes(s.id) || of(s) === path)
      .sort((a, b) => rank(a) - rank(b) || b.at - a.at);
  }, [sessions, ids, of, path]);
  const inSub = (sub: string) => listed(sessions).filter((s) => within(of(s), sub)).length;
  // what waits in it and in each sub-project: the one count of a project («Operator N», its row on the left)
  const waitsIn = useMemo(() => waitingIn(feed, listed(sessions), of), [feed, sessions, of]);

  const questions = data?.sections.filter((s) => s.kind === "questions").flatMap((s) => s.items) ?? [];
  const tasks = data?.sections.filter((s) => s.kind === "tasks").flatMap((s) => s.items) ?? [];
  const other = data?.sections.filter((s) => s.kind === "other" && s.items.length) ?? [];
  const records = data ? NATURES.reduce((n, k) => n + data.records[k].length, 0) : 0;
  const files = (data?.files?.length ?? 0) + (data?.more_files ?? 0);

  // a Space's collections: its first tab's views are the work under the passport on «Overview»; the tabs its views
  // name stand between «Overview» and «Records». A tab of queues alone, all empty now, is not there until one has a row.
  const tabs = views?.tabs ?? [];
  const viewTabs = tabs.filter((t, i) => (i > 0 || placedCount(t.places) > 0) && !(i > 0 && idle(t.places)));
  const work = viewTabs.find((t) => t.title === null);
  const named = viewTabs.filter((t) => t.title !== null);
  // an empty tab is not there (Records, Files); «Operator» always is: the way into the project's requests
  const names = [OVERVIEW, ...named.map((t) => t.title as string), ...(records ? [RECORDS] : []), ...(files ? [FILES] : []), OPERATOR];
  const tab = names.includes(picked) ? picked : OVERVIEW;
  const pick = (name: string) => {
    tabOf.set(path, name);
    setPicked(name);
  };
  const counts: Record<string, number | undefined> = Object.fromEntries(
    named.map((t) => [
      t.title as string,
      t.places.queues.reduce((n, p) => n + (p.view.error === undefined && p.view.kind === "queue" ? p.view.count : 0), 0) || undefined,
    ]),
  );
  counts[OVERVIEW] = work ? actsOf(work.places) || undefined : undefined;
  counts[RECORDS] = records || undefined;
  counts[FILES] = files || undefined;
  counts[OPERATOR] = waitsIn(path) || undefined;
  const current = named.find((t) => t.title === tab);
  const waitingCount = mine.filter(sessionWaits).length;
  const working = mine.filter((s) => s.state === "active" && !s.waiting).length;
  const summary = [waitingCount && `${waitingCount} waiting`, working && `${working} working`].filter(Boolean).join(" · ");
  const parent = data?.parent;
  const back = "hit -ml-1 inline-flex size-8 shrink-0 items-center justify-center self-center rounded-md hover:bg-accent";
  const revs = views?.shown?.collections.map((c) => c.rev).join() ?? "";
  // the views take the list's width; a project of the Base alone reads in a column
  const wideTab = current !== undefined || (tab === OVERVIEW && work !== undefined);

  return (
    <div className="flex flex-col">
      <header className="sticky top-0 z-10 shrink-0 border-b bg-background/95 pt-[env(safe-area-inset-top)] backdrop-blur">
        <div className="mx-auto flex min-h-12 w-full max-w-[90rem] items-stretch gap-1 overflow-x-auto px-3 [scrollbar-width:none] md:px-5">
          {below ? (
            <ToProject path={parent || top} className={back} label="To the project above">
              <ArrowLeft className="size-4" />
            </ToProject>
          ) : (
            <Link to="/spaces" aria-label="To the Spaces" title="To the Spaces" className={cn(back, "lg:hidden")}>
              <ArrowLeft className="size-4" />
            </Link>
          )}
          <div className="flex shrink-0 items-center gap-2 self-center pr-3">
            {sphere?.icon ? <SphereMark icon={sphere.icon} className="size-4 shrink-0 text-muted-foreground" /> : <Layers className="size-4 shrink-0 text-muted-foreground" />}
            <h1 className="max-w-[40vw] truncate text-[15px] font-medium md:max-w-[24rem]">{title}</h1>
            <span className="text-xs whitespace-nowrap text-muted-foreground max-sm:hidden">
              project{subs.length ? ` · ${subs.length} ${plural(subs.length, "sub-project", "sub-projects")}` : ""}
            </span>
          </div>
          <div role="tablist" aria-label="The project" className="flex shrink-0 items-stretch">
            {names.map((name) => (
              <button
                key={name}
                type="button"
                role="tab"
                aria-selected={tab === name}
                onClick={() => pick(name)}
                className={cn(
                  "-mb-px inline-flex items-center gap-1.5 border-b-2 px-2.5 text-[13px] whitespace-nowrap transition-colors",
                  tab === name ? "border-foreground text-foreground" : "border-transparent text-muted-foreground hover:text-foreground",
                )}
              >
                {name}
                {counts[name] !== undefined && (
                  <span className={cn("font-mono text-xs tabular-nums", name === OPERATOR ? "text-warning-foreground dark:text-warning" : "text-muted-foreground")}>
                    {counts[name]}
                  </span>
                )}
              </button>
            ))}
          </div>
          <span className="ml-auto hidden shrink-0 self-center pl-3 text-xs text-muted-foreground md:inline">{summary}</span>
        </div>
      </header>
      {tab === OPERATOR ? (
        operatorView === "map" ? (
          <MapView feed={feed} spheres={spheres} now={now} project={path} head={<OperatorSwitch view={operatorView} onView={pickOperatorView} />} />
        ) : (
          <Operator
            feed={feed}
            spheres={spheres}
            now={now}
            project={path}
            empty="Nothing in this project is asking you for anything."
            head={<OperatorSwitch view={operatorView} onView={pickOperatorView} />}
          />
        )
      ) : (
        <main
          className={cn(
            "mx-auto flex w-full flex-col gap-6 pt-4 pb-[max(1.5rem,env(safe-area-inset-bottom))]",
            wideTab ? "max-w-[90rem] gap-4" : "max-w-4xl",
            PAGE_X,
            "md:px-6",
          )}
        >
          {current && <ViewsTab places={current.places} />}
          {tab === OVERVIEW && (
            <>
              {error && !data && <Empty>{error}</Empty>}
              {!error && !data && <Empty>Loading the project…</Empty>}
              {data && <PassportCard passport={data.passport} rules={data.rules} onEdit={() => setTopic({ passport: true })} />}
              {data && <Yours questions={questions} tasks={tasks} onTopic={setTopic} />}
              {views?.error && !views.shown && <Empty>{`The views did not load: ${views.error}`}</Empty>}
              {views && <SpaceProposals space={path} api={proposals ?? proposalsApi} />}
              {work && <FirstTab places={work.places} />}
              <Section
                title="Sessions"
                count={mine.length}
                action={
                  <Button size="sm" variant="ghost" onClick={() => setTopic({})} aria-label="New session here">
                    <Plus />
                    <span>New</span>
                  </Button>
                }
              >
                {mine.length || subs.length ? (
                  <ul className={LIST} aria-label="Sessions">
                    {mine.map((s) => (
                      <SessionRow key={s.id} s={s} />
                    ))}
                    {subs.map((row) => (
                      <SubProjectRow key={row.name} row={row} waiting={waitsIn(row.name)} sessions={inSub(row.name)} />
                    ))}
                  </ul>
                ) : (
                  <Empty>No sessions here.</Empty>
                )}
              </Section>
              {other.map((s) => (
                <details key={s.heading} className="rounded-lg border px-3 py-2">
                  <summary className="cursor-pointer text-sm font-medium">{s.heading || "Untitled"}</summary>
                  <div className="pt-2">
                    <Lines items={s.items} />
                  </div>
                </details>
              ))}
              {views && (views.shown?.collections.length ?? 0) > 0 && edit && <SpaceChanges space={path} api={edit} revs={revs} />}
            </>
          )}
          {tab === RECORDS && data && <RecordsList space={top} below={below} view={data} />}
          {tab === FILES && data && <ProjectFiles project={path} files={data.files ?? []} more={data.more_files} />}
        </main>
      )}
      <Discuss key={JSON.stringify(topic)} space={top} project={path} title={title} topic={topic} onClose={() => setTopic(null)} />
    </div>
  );
}

function useNowS(everyMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), everyMs);
    return () => clearInterval(timer);
  }, [everyMs]);
  return now;
}

export function SpacePage({
  space,
  read = spacesApi.space,
  views = collectionsApi,
  edit = editApi,
  proposals = proposalsApi,
}: {
  space: string;
  read?: typeof spacesApi.space;
  views?: typeof collectionsApi;
  edit?: EditApi;
  proposals?: ProposalsApi;
}) {
  const shown = useSpaceViews(space, views);
  const tabs = useMemo(() => layout(shown.data), [shown.data]);
  const moves = useMemo(() => movesOf(shown.data), [shown.data]);
  const { data, error } = useRead(`space:${space}`, () => read(space));
  return (
    <Editor api={edit} today={shown.data?.today ?? ""} refresh={shown.refresh} reload={() => void shown.reload()} moves={moves}>
      <ProjectScreen
        path={space}
        data={data?.space}
        error={error}
        ids={data?.sessions ?? []}
        views={{ shown: shown.data, error: shown.error, tabs }}
        edit={edit}
        proposals={proposals}
      />
    </Editor>
  );
}

// --- projects: a link to one, any project's files, a project below a Space ------------------------------------------

/** A link to a project by its folder: `/spaces/<space>`, or `/spaces/<space>/p/<the rest>` below it. */
function ToProject({ path, className, label, children }: { path: string; className?: string; label?: string; children: React.ReactNode }) {
  const [space = "", ...below] = path.split("/");
  return below.length ? (
    <Link to="/spaces/$space/p/$" params={{ space, _splat: below.join("/") }} className={className} aria-label={label} title={label}>
      {children}
    </Link>
  ) : (
    <Link to="/spaces/$space" params={{ space }} className={className} aria-label={label} title={label}>
      {children}
    </Link>
  );
}

/** Every file of a project, each in the row a session's files use: a picture, a recording, a film in place, a PDF or
 * a text opened in place. */
function ProjectFiles({ project, files, more = 0 }: { project: string; files: ProjectFile[]; more?: number }) {
  return (
    <Section title="Files" count={files.length + more}>
      <ul className="flex flex-col gap-2" aria-label="Project files">
        {files.map((f) => {
          const at = project ? `${project}/${f.path}` : f.path;
          const kind = f.big ? "other" : f.kind === "html" ? "text" : f.kind;
          return <FileRow key={f.path} file={{ path: f.path, name: f.path.split("/").pop()!, size: f.size }} kind={kind} src={baseFile(at)} frame={baseView(at)} />;
        })}
      </ul>
      {more > 0 && <Empty>{`and ${more} more — open them on a computer`}</Empty>}
    </Section>
  );
}

/** A project below a Space: the same screen, without the Space's collections. */
export function ProjectPage({ path, read = spacesApi.project }: { path: string; read?: typeof spacesApi.project }) {
  const { data, error } = useRead(`project:${path}`, () => read(path));
  return <ProjectScreen path={path} data={data?.project} error={error} ids={data?.sessions ?? []} />;
}

// --- one Record --------------------------------------------------------------------------------------------------------

const WEB = /(https?:\/\/[^\s<>«»"'()[\]]+[^\s<>«»"'()[\].,;:!?])/;

/** A head's `source` as plain text — never markdown, which the server did not rewrite — with only its http(s)
 * addresses as links, opened in a new tab. */
export function SourceText({ text }: { text: string }) {
  return (
    <p data-testid="source" className="pt-1 break-words whitespace-pre-wrap">
      {text.split(WEB).map((part, i) =>
        i % 2 ? (
          <a key={i} href={part} target="_blank" rel="noopener noreferrer nofollow" className="text-primary underline underline-offset-2">
            {part}
          </a>
        ) : (
          part
        ),
      )}
    </p>
  );
}

function RecordBody({ record }: { record: RecordView }) {
  const { main, more, extra } = headWords(record.head);
  const type = typeof record.head.type === "string" ? record.head.type : "";
  const source = record.head.source;
  const { title, rest } = ownTitle(record.body);
  return (
    <article className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <h2 className="text-xl leading-tight font-semibold">{title ?? record.title}</h2>
        <p data-testid="head-words" className="flex flex-wrap items-center gap-x-1 text-sm text-muted-foreground">
          {type && <Badge variant="outline">{TYPE_WORDS[type] ?? type}</Badge>}
          {main.map((p, i) => (
            <span key={p.key} className={cn(p.warn && "font-medium text-amber-700 dark:text-warning")}>
              {i > 0 && "· "}
              {p.text}
            </span>
          ))}
        </p>
        {more.length > 0 && <p className="text-xs text-muted-foreground">{more.map((p) => p.text).join(" · ")}</p>}
        <p className="font-mono text-xs break-all text-muted-foreground">
          {record.space}/{record.path}
        </p>
      </div>
      {source !== undefined && source !== null && source !== "" && (
        <details className="rounded-lg border px-3 py-2 text-sm">
          <summary className="cursor-pointer font-medium">Source</summary>
          <SourceText text={typeof source === "string" ? source : JSON.stringify(source)} />
        </details>
      )}
      {record.bad_head && (
        <p role="status" className="rounded-md bg-warning/20 px-2 py-1 text-sm">
          The Record's head does not parse — showing the file as it is.
        </p>
      )}
      <BaseMarkdown text={rest} />
      {extra.length > 0 && (
        <dl data-testid="head-extra" className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 border-t pt-2 text-xs text-muted-foreground">
          {extra.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="font-mono">{k}</dt>
              <dd className="break-words">{v}</dd>
            </div>
          ))}
        </dl>
      )}
    </article>
  );
}

/** One Record. `pane`: shown beside a session on a wide screen — no header of its own but «Discuss». */
export function RecordPage({ space, path, pane = false, read = spacesApi.record }: { space: string; path: string; pane?: boolean; read?: typeof spacesApi.record }) {
  const spheres = useSpheres();
  const sphere = sphereOf(spheres, space);
  const { data, error } = useRead(`record:${space}/${path}`, () => read(space, path));
  const [topic, setTopic] = useState<Topic | null>(null);
  const record = data?.record;
  const discuss = record && (
    <Button size="sm" variant="outline" onClick={() => setTopic({ record: { path: record.path, title: record.title } })}>
      <MessageCircle />
      Discuss
    </Button>
  );
  return (
    <div className="flex flex-col">
      {pane ? (
        <div className="flex justify-end p-2">{discuss}</div>
      ) : (
        <Header up={{ space, label: `To the Space “${spaceTitle(space, sphere)}”` }} title={spaceTitle(space, sphere)} width="max-w-[68ch]">
          {discuss}
        </Header>
      )}
      <main className={cn("mx-auto w-full max-w-[68ch] pt-4 pb-[max(1rem,env(safe-area-inset-bottom))]", PAGE_X)}>
        {record ? <RecordBody record={record} /> : <Empty>{error ?? "Loading the Record…"}</Empty>}
      </main>
      <Discuss key={JSON.stringify(topic)} space={space} project={record?.project ?? space} topic={topic} onClose={() => setTopic(null)} />
    </div>
  );
}

