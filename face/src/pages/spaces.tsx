// «Spaces»: the Base in the app, read only (retinue/spaces.py). `/spaces` — the grid of Spaces with what waits
// in each; `/spaces/<space>` — its Showcase as a screen: its sessions, Open questions, Tasks, Records by nature with a
// search; `/spaces/<space>/r/<path>` — one Record to read, its head in plain words. «Discuss» starts a session in
// the Space's sphere with the Record or the question in its first message: the Base changes only through a session.
import { Link, useNavigate } from "@tanstack/react-router";
import { Fragment, useEffect, useMemo, useState } from "react";
import { BaseMarkdown } from "@/components/base-markdown";
import { ArrowLeft, ChevronRight, MessageCircle, Plus } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api } from "@/lib/api";
import { plural, sessionStateWord, when } from "@/lib/format";
import { live, useLiveState } from "@/lib/live";
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
import { cn, inputId } from "@/lib/utils";

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

/** For tests: forget what was read. */
export function forgetRead() {
  seen.clear();
}

function useSpheres(): Sphere[] {
  useLiveState(); // meta arrives with the first snapshot
  return live.meta?.spheres ?? NO_SPHERES;
}

// --- pieces ----------------------------------------------------------------------------------------------------------

function Header({ up, title, children }: { up?: { space?: string; label: string }; title: string; children?: React.ReactNode }) {
  const back = "inline-flex size-11 shrink-0 items-center justify-center rounded-md hover:bg-accent";
  return (
    <header className="sticky top-0 z-10 flex min-h-14 items-center gap-1 border-b bg-background/95 px-2 pt-[env(safe-area-inset-top)] backdrop-blur">
      {up?.space ? (
        <Link to="/spaces/$space" params={{ space: up.space }} aria-label={up.label} title={up.label} className={back}>
          <ArrowLeft className="size-5" />
        </Link>
      ) : up ? (
        <Link to="/spaces" aria-label={up.label} title={up.label} className={back}>
          <ArrowLeft className="size-5" />
        </Link>
      ) : null}
      <h1 className={cn("min-w-0 flex-1 truncate font-semibold", !up && "px-2 text-lg")}>{title}</h1>
      {children}
    </header>
  );
}

function Section({ title, count, children, action }: { title: string; count?: number; children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-1">
      <div className="flex min-h-9 items-center px-1">
        <h2 className="flex-1 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
          {title}
          {count !== undefined && <span className="tabular-nums"> ({count})</span>}
        </h2>
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
  const live_ = useMemo(() => sessionBadges(Object.values(sessions)), [sessions]);
  const loaded = Object.keys(sessions).length > 0;
  const rows = useMemo(() => orderSpaces(data?.spaces ?? [], spheres), [data, spheres]);
  const badges = (row: SpaceRow) => (loaded ? (live_.get(row.name) ?? { waiting: 0, working: 0 }) : row);
  return (
    <div className="flex flex-col">
      <Header title="Spaces" />
      <main className="mx-auto w-full max-w-3xl p-3 pb-[max(1rem,env(safe-area-inset-bottom))]">
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
                    <span aria-hidden className="text-xl leading-none">
                      {sphere?.icon || "📁"}
                    </span>
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
    <ul className="flex flex-col gap-1">
      {items.map((item, i) => (
        <li key={i} className={cn("flex items-start gap-2 rounded-lg px-2 py-1.5", item.list ? "border bg-card" : "text-muted-foreground")}>
          <BaseMarkdown text={item.text} className="min-w-0 flex-1" />
          {item.list && onDiscuss && (
            <Button size="sm" variant="ghost" className="shrink-0" onClick={() => onDiscuss(item)} aria-label="Discuss the question">
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

export function SpacePage({ space, read = spacesApi.space }: { space: string; read?: typeof spacesApi.space }) {
  const { sessions } = useLiveState();
  const spheres = useSpheres();
  const sphere = sphereOf(spheres, space);
  const { data, error } = useRead(`space:${space}`, () => read(space));
  const [topic, setTopic] = useState<Topic | null>(null);
  const view = data?.space;
  const mine = useMemo(() => sessionsOf(Object.values(sessions), space, Date.now() / 1000), [sessions, space]);
  const waiting = mine.filter((s) => s.state === "waiting" || s.waiting > 0).length;
  const working = mine.filter((s) => s.state === "active" && !s.waiting).length;
  const questions = view?.sections.filter((s) => s.kind === "questions").flatMap((s) => s.items) ?? [];
  const tasks = view?.sections.filter((s) => s.kind === "tasks").flatMap((s) => s.items) ?? [];
  const other = view?.sections.filter((s) => s.kind === "other" && s.items.length) ?? [];
  const count = (items: ShowcaseItem[]) => items.filter((i) => i.list).length;
  const summary = [
    waiting && `${waiting} waiting`,
    working && `${working} working`,
    count(questions) && `${count(questions)} ${plural(count(questions), "Open question", "Open questions")}`,
  ].filter(Boolean);
  return (
    <div className="flex flex-col">
      <Header up={{ label: "To the Spaces" }} title={`${sphere?.icon ? `${sphere.icon} ` : ""}${spaceTitle(space, sphere, view?.title)}`} />
      <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-3 pb-[max(1rem,env(safe-area-inset-bottom))]">
        {summary.length > 0 && <p className="px-1 text-sm text-muted-foreground">{summary.join(" · ")}</p>}
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
      </main>
      <Discuss key={JSON.stringify(topic)} space={space} topic={topic} onClose={() => setTopic(null)} />
    </div>
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
        <Header up={{ space, label: `To the Space “${spaceTitle(space, sphere)}”` }} title={spaceTitle(space, sphere)}>
          {discuss}
        </Header>
      )}
      <main className="mx-auto w-full max-w-[68ch] p-3 pb-[max(1rem,env(safe-area-inset-bottom))]">
        {record ? <RecordBody record={record} /> : <Empty>{error ?? "Loading the Record…"}</Empty>}
      </main>
      <Discuss key={JSON.stringify(topic)} space={space} topic={topic} onClose={() => setTopic(null)} />
    </div>
  );
}

