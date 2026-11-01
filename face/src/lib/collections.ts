// A Space's views of its collections (docs/collections.md#views): the numbers, the queues and the boards its screen
// shows above the text, read only. The platform computes every view; here they are only put in words and in place.
// A collection's view is read again when the stream says its `rev` moved on, and all of them after a resync.
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, request } from "./api";
import { live as theLive, type Live } from "./live";
import { day } from "./spaces";
import type { CollectionEvent } from "./types";
import type { BoardView, CollectionViews, Labels, Moment, OneView, SpaceViews, StatView, View, ViewRow } from "./view-types";

export * from "./view-types";

const seg = (s: string) => encodeURIComponent(s);

export const collectionsApi = {
  space: (space: string) => request<SpaceViews>(`/api/c/_space?space=${seg(space)}`),
  view: (collection: string, view: string) => request<OneView>(`/api/c/${seg(collection)}?view=${seg(view)}`),
};

// --- in place ---------------------------------------------------------------------------------------------------

export interface Placed<V extends View = View> {
  collection: CollectionViews;
  view: V;
}

/** One tab's places: its numbers, queues, boards, agendas and charts — each in the order the collections come in
 * (by name) and their views within them. A broken view stays in the place of its kind. */
export interface Places {
  stats: Placed[];
  queues: Placed[];
  boards: Placed[];
  agendas: Placed[];
  charts: Placed[];
}

const KIND_PLACE = { stat: "stats", queue: "queues", board: "boards", agenda: "agendas", chart: "charts" } as const;

export const NO_PLACES: Places = { stats: [], queues: [], boards: [], agendas: [], charts: [] };

/** The screen's tabs, in the order their first view comes in: the views without a `tab` in the first (`title` null),
 * which is there even when it has none. */
export function layout(answer: SpaceViews | null | undefined): { title: string | null; places: Places }[] {
  const tabs = new Map<string | null, Places>([[null, { stats: [], queues: [], boards: [], agendas: [], charts: [] }]]);
  for (const collection of answer?.collections ?? []) {
    for (const view of collection.views) {
      const key = view.tab ?? null;
      if (!tabs.has(key)) tabs.set(key, { stats: [], queues: [], boards: [], agendas: [], charts: [] });
      tabs.get(key)![KIND_PLACE[view.kind]].push({ collection, view });
    }
  }
  return [...tabs].map(([title, places]) => ({ title, places }));
}

/** How many views a tab's places hold. */
export const placedCount = (p: Places) => p.stats.length + p.queues.length + p.boards.length + p.agendas.length + p.charts.length;

const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const ULID = /^[0-9A-HJKMNP-TV-Z]{26}$/;

/** A ULID's moment, in ms; null for anything else. */
export function ulidTime(id: string | null | undefined): number | null {
  if (!id || !ULID.test(id)) return null;
  let ms = 0;
  for (const ch of id.slice(0, 10)) ms = ms * 32 + CROCKFORD.indexOf(ch);
  return ms;
}

/** Whether `rev` is past `known`: ULIDs grow, so as strings; anything is past none. */
export function newer(rev: string | null | undefined, known: string | null | undefined): boolean {
  return !!rev && (!known || rev > known);
}

const isoDay = (ms: number) => new Date(ms).toISOString().slice(0, 10);

/** What an empty view says instead of nothing: since when the collection is quiet, or that it never had a record. */
export function emptyWords(collection: CollectionViews, now: Date = new Date()): string {
  const at = ulidTime(collection.rev);
  return at === null ? "No data yet: nothing was written here." : `Nothing here — no change since ${day(isoDay(at), now)}.`;
}

/** An enum's value as the owner reads it: the form's words for it when it gives some, else `pack_ready` → «Pack
 * ready»; none → «Unknown». */
export function valueWords(value: string | null, labels?: Record<string, string>): string {
  if (value === null) return "Unknown";
  if (labels?.[value]) return labels[value];
  const text = value.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** One field of a row in words, or null when there is nothing to say: a ref by its record's words (never its id),
 * an enum's value by the form's words (`labels`), a day as «21.09», a number with its thousands marked, a yes as the
 * field's own name. */
export function cell(row: ViewRow, field: string, now: Date = new Date(), labels: Labels = {}): string | null {
  if (row.refs[field]) return row.refs[field];
  const value = row.fields[field];
  if (value === undefined || value === null || value === "" || value === false) return null;
  if (value === true) return valueWords(field);
  if (typeof value === "number") return value.toLocaleString("en-US");
  if (typeof value !== "string" || ULID.test(value)) return null;
  if (labels[field]) return valueWords(value, labels[field]);
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? day(value, now) : value;
}

/** A row's head and the rest of its words, in the view's order of fields; one with none at all is «Untitled». */
export function rowWords(row: ViewRow, fields: string[], now: Date = new Date(), labels: Labels = {}): { title: string; rest: string[] } {
  const words = fields.map((f) => cell(row, f, now, labels)).filter((w): w is string => w !== null);
  return { title: words[0] ?? "Untitled", rest: words.slice(1) };
}

/** A row as a line of a list shows it: its head, the field beside it, the line under it, and the enums at its right —
 * each field by its place in the view's `fields` (the first three) and the enums after them. */
export function lineWords(row: ViewRow, fields: string[], labels: Labels = {}, now: Date = new Date()) {
  const say = (f: string | undefined) => (f === undefined ? null : cell(row, f, now, labels));
  const [head, beside, under, ...rest] = fields;
  const first = say(head);
  return {
    // a row without its head is headed by the field beside it
    title: first ?? say(beside) ?? "Untitled",
    beside: first === null ? null : say(beside),
    under: say(under),
    tags: rest.map(say).filter((w): w is string => w !== null),
  };
}

/** The days in words — «today», «4 days», «in 3 days» — and, past the threshold, the word that says so as well as
 * the colour: a board's card is «stuck», a queue's row «overdue». */
export function daysWords(row: Pick<ViewRow, "days" | "rotten">, kind: "board" | "queue"): { text: string; late: string | null } {
  const d = row.days;
  const text = d === null ? "" : d === 0 ? "today" : d < 0 ? `in ${-d} ${-d === 1 ? "day" : "days"}` : `${d} ${d === 1 ? "day" : "days"}`;
  return { text, late: row.rotten ? (kind === "board" ? "stuck" : "overdue") : null };
}

/** How far the number is to its goal: the share filled (0 to 1) and whether it is met; null without a goal. */
export function progress(stat: Pick<StatView, "value" | "goal">): { share: number; met: boolean } | null {
  if (stat.goal === null || stat.goal <= 0) return null;
  return { share: Math.max(0, Math.min(1, stat.value / stat.goal)), met: stat.value >= stat.goal };
}

/** The tile's window in words: «this week, 26.10–01.11», «this month, 01.11–30.11», «all time». */
export function windowWords(stat: Pick<StatView, "window" | "first" | "last">, now: Date = new Date()): string {
  if (!stat.window || !stat.first || !stat.last) return "all time";
  return `this ${stat.window}, ${day(stat.first, now)}–${day(stat.last, now)}`;
}

/** The period before, in words: «last week: 2». */
export function previousWords(stat: Pick<StatView, "window" | "previous">): string | null {
  return stat.window && stat.previous !== null ? `last ${stat.window}: ${stat.previous}` : null;
}

/** Fewer than three records make no board: they read better as a list. */
export const BOARD_MIN = 3;

export function boardTotal(board: Pick<BoardView, "columns">): number {
  return board.columns.reduce((n, c) => n + c.count, 0);
}

/** The column a phone opens on: the first open one with a card stuck in it, else the first open one with cards,
 * else the first. */
export function openColumn(board: Pick<BoardView, "columns">): number {
  const open = board.columns.map((c, i) => ({ c, i })).filter(({ c }) => !c.folded);
  const pick = open.find(({ c }) => c.rows.some((r) => r.rotten)) ?? open.find(({ c }) => c.count > 0) ?? open[0];
  return pick?.i ?? 0;
}

// --- live -------------------------------------------------------------------------------------------------------

const kept = new Map<string, SpaceViews | null>();

/** For tests: forget the views read. */
export function forgetViews() {
  kept.clear();
}

type ViewsApi = Pick<typeof collectionsApi, "space" | "view">;

/** One collection's views read again, after the stream told of a newer `rev`, and the oldest `rev` among them — so
 * an event past it reads them again; null when one of them is not there any more (a define took it away): the Space
 * is read anew. */
async function reread(api: ViewsApi, collection: CollectionViews): Promise<{ views: View[]; rev: string | null; moment: Moment | null } | null> {
  try {
    const answers = await Promise.all(collection.views.map((v) => api.view(collection.name, v.name)));
    const rev = answers.reduce<string | null>((low, a) => (low === null || (a.rev !== null && a.rev < low) ? a.rev : low), null);
    const last = answers[answers.length - 1];
    return { views: answers.map((a) => a.view), rev, moment: last ? { tz: last.tz, today: last.today, at: last.at } : null };
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

/** A Space's views: read on mount, when the app comes back to the front and after the stream resynced; a
 * collection's alone again when the stream says it moved past the `rev` shown. A collection the Space did not have
 * (a define brought it in) reads the Space anew. A Space without collections is `{collections: []}`; a platform
 * that has no such route (404) is the same: the screen shows its text alone. */
export function useSpaceViews(space: string, api: ViewsApi = collectionsApi, source: Live = theLive) {
  const [data, setData] = useState<SpaceViews | null>(() => kept.get(space) ?? null);
  const [error, setError] = useState<string | null>(null);
  const current = useRef(data);
  const turn = useRef(0);
  const put = useCallback(
    (next: SpaceViews | null) => {
      current.current = next;
      kept.set(space, next);
      setData(next);
    },
    [space],
  );
  const load = useCallback(async () => {
    const mine = ++turn.current;
    try {
      const answer = await api.space(space);
      if (mine === turn.current) {
        put(answer);
        setError(null);
      }
    } catch (e) {
      if (mine !== turn.current) return;
      if (e instanceof ApiError && e.status === 404) put({ space, tz: "", today: "", at: 0, collections: [] });
      else setError(e instanceof ApiError ? e.message : "Did not load.");
    }
  }, [api, space, put]);
  const moved = useCallback(
    async ({ name, space: theirs, rev }: CollectionEvent) => {
      if (!current.current) return;
      const known = current.current.collections.find((c) => c.name === name);
      if (!known || theirs !== space) {
        if (known || theirs === space) void load(); // one that came into this Space, or left it
        return;
      }
      if (!newer(rev, known.rev)) return;
      const mine = turn.current;
      try {
        const fresh = await reread(api, known);
        if (mine !== turn.current) return; // the whole Space was read meanwhile
        if (fresh === null) return void load();
        const now = current.current;
        const was = now?.collections.find((c) => c.name === name);
        if (!now || !was || !newer(fresh.rev, was.rev)) return; // a later read got there first
        put({
          ...now,
          ...fresh.moment,
          collections: now.collections.map((c) => (c.name === name ? { ...c, rev: fresh.rev, views: fresh.views } : c)),
        });
      } catch {
        // the next event, the next visit or the next resync reads it again
      }
    },
    [api, space, load, put],
  );
  useEffect(() => {
    current.current = kept.get(space) ?? null;
    setData(current.current);
    void load();
    const onVisible = () => {
      if (!document.hidden) void load();
    };
    document.addEventListener("visibilitychange", onVisible);
    const offs = [source.onCollection((e) => void moved(e)), source.onResync(() => void load())];
    return () => {
      turn.current += 1;
      document.removeEventListener("visibilitychange", onVisible);
      for (const off of offs) off();
    };
  }, [space, source, load, moved]);
  /** What the owner's own write made, read at once rather than when the stream tells of it. */
  const refresh = useCallback(
    (name: string, rev: string | null) => {
      if (rev) void moved({ name, space, rev });
    },
    [moved, space],
  );
  return { data, error, reload: load, refresh };
}
