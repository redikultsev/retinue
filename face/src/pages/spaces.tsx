// «Spaces»: the Base in the app, read only (retinue/spaces.py). `/spaces` — the grid of Spaces with what waits
// in each; `/spaces/<space>` — its Showcase as a screen: its sessions, Open questions, Tasks, Records by nature with a
// search; `/spaces/<space>/r/<path>` — one Record to read, its head in plain words. «Discuss» starts a session in
// the Space's sphere with the Record or the question in its first message: the Base changes only through a session.
import { PageHeader, PAGE_X } from "@/components/page-header";
import { Link, useNavigate } from "@tanstack/react-router";
import { Fragment, useEffect, useMemo, useState } from "react";
import { BaseMarkdown } from "@/components/base-markdown";
import { Agenda } from "@/components/views/agenda";
import { Board } from "@/components/views/board";
import { SpaceChanges } from "@/components/views/changes";
import { Chart } from "@/components/views/chart";
import { AddButton, Editor, type Move } from "@/components/views/edit";
import { SpaceProposals } from "@/components/views/proposals";
import { Queue } from "@/components/views/queue";
import { StatList, StatStrip, statsWindow } from "@/components/views/stat";
import { Strip } from "@/components/views/strip";
import { ArrowLeft, ChevronRight, MessageCircle, Plus, SphereMark } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api } from "@/lib/api";
import { collectionsApi, layout, type Placed, type Places, placedCount, type SpaceViews, useSpaceViews } from "@/lib/collections";
import { type EditApi, editApi, type ProposalsApi, proposalsApi } from "@/lib/edit";
import { plural, sessionStateWord, when } from "@/lib/format";
import { live, useLiveState } from "@/lib/live";
import { listed } from "@/lib/store";
import {
  discussText,
  headWords,
  NATURE_WORDS,
  NATURES,
  type Nature,
  orderSpaces,
  ownTitle,
  type RecordRow,
  type RecordView,
  rowWords,
  type ShowcaseItem,
  type SpaceRow,
  type SpaceView,
  sessionBadges,
  sessionsOf,
  spacesApi,
  TYPE_WORDS,
} from "@/lib/spaces";
import { toast } from "@/lib/toast";
import type { Session, Sphere } from "@/lib/types";
import { cn, inputId, LIST } from "@/lib/utils";

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

type Topic = { record?: { path: string; title: string }; question?: string };

/** A new session in the Space's sphere (its track), its first message naming the Record or quoting the question; the
 * owner adds his own words. A Space no sphere names: the owner picks the track. */
export function Discuss({ space, topic, onClose, start = api.newSession }: { space: string; topic: Topic | null; onClose: () => void; start?: typeof api.newSession }) {
  const spheres = useSpheres();
  const sphere = sphereOf(spheres, space);
  const tracks = live.meta?.tracks ?? [];
  const [track, setTrack] = useState(sphere?.track ?? tracks[0]?.name ?? "");
  const [own, setOwn] = useState("");
  const [busy, setBusy] = useState(false);
  const [id, setId] = useState(inputId);
  const navigate = useNavigate();
  const text = topic ? discussText(space, topic, own) : "";
  const chosen = sphere?.track ?? track;

  async function submit() {
    if (!chosen || !text.trim()) return;
    setBusy(true);
    try {
      const res = await start(chosen, text, id, sphere?.name ?? null);
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

  const what = topic?.record ? "Record" : topic?.question ? "Open question" : "";
  return (
    <Dialog open={topic !== null} onClose={onClose} title={what ? `Discuss: ${what.toLowerCase()}` : "New session"}>
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <p className="text-sm text-muted-foreground">
          New session in the Space “{spaceTitle(space, sphere)}”{what ? ", the first message will be:" : "."}
        </p>
        {what && (
          <blockquote data-testid="discuss-head" className="max-h-40 overflow-y-auto rounded-md border bg-muted/40 p-2 text-sm whitespace-pre-wrap">
            {discussText(space, topic ?? {}, "")}
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
          <span className="text-sm font-medium">{what ? "What should I ask or do?" : "What should I do?"}</span>
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
                <Link
                  to="/spaces/$space"
                  params={{ space: row.name }}
                  className="flex h-full min-h-24 flex-col gap-2 rounded-xl border bg-card p-3 hover:bg-accent"
                >
                  <span className="flex items-center gap-2">
                    <SphereMark icon={sphere?.icon || "📁"} className="size-4 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1 truncate font-medium">{spaceTitle(row.name, sphere, row.title)}</span>
                  </span>
                  <Badges waiting={waiting} working={working} questions={row.questions} />
                </Link>
              </li>
            );
          })}
        </ul>
      </main>
    </div>
  );
}

// --- one Space ---------------------------------------------------------------------------------------------------------

function SessionRow({ s }: { s: Session }) {
  const waits = s.state === "waiting" || s.waiting > 0;
  return (
    <li>
      <Link to="/s/$id" params={{ id: s.id }} className="flex min-h-12 items-center gap-3 rounded-lg px-2 py-2 hover:bg-accent">
        <span
          className={cn("size-2 shrink-0 rounded-full", waits ? "bg-warning" : s.state === "active" ? "animate-pulse bg-primary" : "bg-muted-foreground/40")}
        />
        <span className="min-w-0 flex-1">
          <span className={cn("block truncate text-[15px] md:text-sm", s.unread && "font-semibold")}>{s.title || s.track}</span>
          <span className="block truncate text-xs text-muted-foreground">
            {waits ? "waiting for you" : sessionStateWord(s.state)} · {when(s.at)}
          </span>
        </span>
        {s.waiting > 0 && <Badge variant="warning">{s.waiting}</Badge>}
      </Link>
    </li>
  );
}

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

function RecordLink({ space, nature, row }: { space: string; nature: Nature; row: RecordRow }) {
  const words = rowWords(row, nature);
  return (
    <li>
      <Link
        to="/spaces/$space/r/$"
        params={{ space, _splat: row.path }}
        className="flex min-h-12 items-center gap-2 rounded-lg px-2 py-2 hover:bg-accent"
      >
        <span className="min-w-0 flex-1">
          <span className="block text-[15px] leading-snug md:text-sm">{row.title}</span>
          {(words.length > 0 || row.big) && (
            <span className="block truncate text-xs text-muted-foreground">
              {words.map((w, i) => (
                <span key={w.key} className={cn(w.warn && "text-amber-700 dark:text-warning")}>
                  {i > 0 && " · "}
                  {w.text}
                </span>
              ))}
              {row.big && `${words.length ? " · " : ""}large — on a computer`}
            </span>
          )}
        </span>
        <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
      </Link>
    </li>
  );
}

function Records({ space, view }: { space: string; view: SpaceView }) {
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const shown = (row: RecordRow) => !q || row.title.toLowerCase().includes(q) || row.path.toLowerCase().includes(q);
  const total = NATURES.reduce((n, k) => n + view.records[k].length, 0);
  const found = NATURES.map((nature) => ({ nature, rows: view.records[nature].filter(shown) })).filter((g) => g.rows.length);
  return (
    <Section title="Records" count={total}>
      <p className="px-1 text-sm text-muted-foreground">
        {NATURES.filter((n) => view.records[n].length)
          .map((n) => `${NATURE_WORDS[n]} ${view.records[n].length}`)
          .join(" · ") || "No Records."}
      </p>
      {total > 0 && (
        <Input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Find a Record" aria-label="Find a Record" className="my-1" />
      )}
      {q && found.length === 0 && <Empty>Nothing found.</Empty>}
      {found.map(({ nature, rows }) => (
        <div key={nature} className="flex flex-col">
          <h3 className="px-1 pt-2 text-sm font-medium">{NATURE_WORDS[nature]}</h3>
          <ul className="flex flex-col">
            {rows.map((row, i) => (
              <Fragment key={row.path}>
                {row.folder && row.folder !== rows[i - 1]?.folder && (
                  <li className="px-2 pt-1 font-mono text-xs text-muted-foreground">{row.folder}/</li>
                )}
                <RecordLink space={space} nature={nature} row={row} />
              </Fragment>
            ))}
          </ul>
        </div>
      ))}
    </Section>
  );
}

/** A board or a queue in its own section; one the platform could not make says why in its place. */
function Shown({ placed }: { placed: Placed }) {
  const { collection, view } = placed;
  if (view.error === undefined && view.kind === "chart") return <Chart collection={collection} view={view} />; // titled inside
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

/** The tab a Space was left on, kept while the app is open. */
const tabOf = new Map<string, string>();

const BASE_TAB = "Base";

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
  const { sessions } = useLiveState();
  const shown = useSpaceViews(space, views);
  const tabs = useMemo(() => layout(shown.data), [shown.data]);
  const moves = useMemo(() => movesOf(shown.data), [shown.data]);
  const spheres = useSpheres();
  const sphere = sphereOf(spheres, space);
  const { data, error } = useRead(`space:${space}`, () => read(space));
  const [topic, setTopic] = useState<Topic | null>(null);
  const [picked, setPicked] = useState<string>(() => tabOf.get(space) ?? "");
  const view = data?.space;
  const mine = useMemo(() => sessionsOf(listed(sessions), space, Date.now() / 1000), [sessions, space]);
  const waiting = mine.filter((s) => s.state === "waiting" || s.waiting > 0).length;
  const working = mine.filter((s) => s.state === "active" && !s.waiting).length;
  const questions = view?.sections.filter((s) => s.kind === "questions").flatMap((s) => s.items) ?? [];
  const tasks = view?.sections.filter((s) => s.kind === "tasks").flatMap((s) => s.items) ?? [];
  const other = view?.sections.filter((s) => s.kind === "other" && s.items.length) ?? [];
  const count = (items: ShowcaseItem[]) => items.filter((i) => i.list).length;
  const summary = [waiting && `${waiting} waiting`, working && `${working} working`].filter(Boolean);
  const revs = shown.data?.collections.map((c) => c.rev).join() ?? "";
  // A Space without collections has only its Base: it opens there, as it always did. A tab of queues alone, all empty
  // now — drafts with none waiting — is not there until one has a row.
  const viewTabs = tabs.filter((t, i) => (i > 0 || placedCount(t.places) > 0) && !(i > 0 && idle(t.places)));
  const names = [...viewTabs.map((t) => t.title ?? "Now"), BASE_TAB];
  const tab = names.includes(picked) ? picked : names[0]!;
  const pick = (name: string) => {
    tabOf.set(space, name);
    setPicked(name);
  };
  const counts: Record<string, number | undefined> = Object.fromEntries(
    viewTabs.map((t) => [t.title ?? "Now", t.title === null ? actsOf(t.places) || undefined : t.places.queues.reduce((n, p) => n + (p.view.error === undefined && p.view.kind === "queue" ? p.view.count : 0), 0) || undefined]),
  );
  counts[BASE_TAB] = count(questions) || undefined;
  const current = viewTabs.find((t) => (t.title ?? "Now") === tab);
  const title = spaceTitle(space, sphere, view?.title);
  return (
    <Editor api={edit} today={shown.data?.today ?? ""} refresh={shown.refresh} reload={() => void shown.reload()} moves={moves}>
    <div className="flex flex-col">
      <header className="sticky top-0 z-10 shrink-0 border-b bg-background/95 pt-[env(safe-area-inset-top)] backdrop-blur">
        <div className="mx-auto flex min-h-12 w-full max-w-[90rem] items-stretch gap-1 overflow-x-auto px-3 [scrollbar-width:none] md:px-5">
          <Link
            to="/spaces"
            aria-label="To the Spaces"
            title="To the Spaces"
            className="hit inline-flex size-8 shrink-0 items-center justify-center self-center rounded-md hover:bg-accent"
          >
            <ArrowLeft className="size-4" />
          </Link>
          <h1 className="flex max-w-[40vw] shrink-0 items-center gap-2 self-center truncate pr-3 text-[15px] font-medium md:max-w-none">
            <SphereMark icon={sphere?.icon} className="size-4 shrink-0 text-muted-foreground" />
            <span className="truncate">{title}</span>
          </h1>
          <div role="tablist" aria-label="Views of the Space" className="flex shrink-0 items-stretch">
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
                {counts[name] !== undefined && <span className="font-mono text-xs text-muted-foreground tabular-nums">{counts[name]}</span>}
              </button>
            ))}
          </div>
          <span className="ml-auto hidden shrink-0 self-center pl-3 text-xs text-muted-foreground md:inline">{summary.join(" · ")}</span>
        </div>
      </header>
      <main
        className={cn(
          "mx-auto flex w-full flex-col gap-4 pt-3 pb-[max(1rem,env(safe-area-inset-bottom))]",
          tab === BASE_TAB ? "max-w-3xl" : "max-w-[90rem]",
          PAGE_X,
          "md:px-5",
        )}
      >
        {shown.error && !shown.data && <Empty>{`The views did not load: ${shown.error}`}</Empty>}
        {tab !== BASE_TAB && current && (
          <>
            {current.title === null && <SpaceProposals space={space} api={proposals} />}
            {current.title === null ? (
              <FirstTab places={current.places} />
            ) : (
              <ViewsTab places={current.places} />
            )}
          </>
        )}
        {tab === BASE_TAB && (
          <>
            {viewTabs.length === 0 && <SpaceProposals space={space} api={proposals} />}
            {error && !view && <Empty>{error}</Empty>}
            {!error && !view && <Empty>Loading the Showcase…</Empty>}
            {view && (
              <>
                <Section title="Open questions" count={count(questions)}>
                  {questions.length ? (
                    <Lines items={questions} onDiscuss={(item) => setTopic({ question: item.text })} />
                  ) : (
                    <Empty>No Open questions.</Empty>
                  )}
                </Section>
                <Section title="Tasks" count={count(tasks)}>
                  {tasks.length ? <Lines items={tasks} /> : <Empty>No Tasks.</Empty>}
                </Section>
                <Records space={space} view={view} />
                {other.map((s) => (
                  <details key={s.heading} className="rounded-lg border px-3 py-2">
                    <summary className="cursor-pointer text-sm font-medium">{s.heading || "Untitled"}</summary>
                    <div className="pt-2">
                      <Lines items={s.items} />
                    </div>
                  </details>
                ))}
                {view.rules && (
                  <details className="rounded-lg border px-3 py-2">
                    <summary className="cursor-pointer text-sm font-medium">Rules of the Space</summary>
                    <BaseMarkdown text={view.rules} className="pt-2" />
                  </details>
                )}
              </>
            )}
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
              {mine.length ? (
                <ul className="flex flex-col">
                  {mine.map((s) => (
                    <SessionRow key={s.id} s={s} />
                  ))}
                </ul>
              ) : (
                <Empty>No sessions here.</Empty>
              )}
            </Section>
            {(shown.data?.collections.length ?? 0) > 0 && <SpaceChanges space={space} api={edit} revs={revs} />}
          </>
        )}
      </main>
      <Discuss key={JSON.stringify(topic)} space={space} topic={topic} onClose={() => setTopic(null)} />
    </div>
    </Editor>
  );
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
      <Discuss key={JSON.stringify(topic)} space={space} topic={topic} onClose={() => setTopic(null)} />
    </div>
  );
}

