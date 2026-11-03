// «Waiting for you», counted once (docs/face.md «Today», «The operator», «A project»): which of the feed's rows «Today»
// lists — a person's ask is Tasks' («From people», lib/asks.ts) —, which of them wait on the owner and which are only
// to read, the sessions' requests the operator, the map and the microphone go by, and how many wait — overall, the one
// number of «Today N», the header, the left column, the tab title, the app badge and «New»; and in a project with its
// sub-projects, the number of its «Operator» tab, its row in the left column and its row on its parent's page.
import { useCallback, useSyncExternalStore } from "react";
import { isAsk } from "./asks";
import { type Feed, type FeedRow, type FeedSource, feedSource } from "./feed";
import { live as theLive } from "./live";
import { endingOf } from "./operator";
import { type ProjectOf, within } from "./projects";
import type { State } from "./store";
import type { Session } from "./types";

const rowsOf = (feed: Feed | null): FeedRow[] => (feed?.groups ?? []).flatMap((g) => g.rows);

/** Whether a row is the owner's move: all but a finished turn, which is only to read. A turn that ended on a thing to
 * look at waits («Looks right»), though the feed lists it under «Read». */
export function waits(row: FeedRow): boolean {
  const e = endingOf(row);
  return e ? e.kind !== "done" : row.action !== "read";
}

/** «Today»'s rows, in the feed's order: all but the people's asks. */
export const todayRows = (feed: Feed | null): FeedRow[] => rowsOf(feed).filter((r) => !isAsk(r));

/** «Today N»: the feed's count less the people's asks, which are Tasks'. */
export function todayCount(feed: Feed | null): number {
  if (!feed) return 0;
  return Math.max(0, feed.count - rowsOf(feed).filter(isAsk).length);
}

const sessionWaits = (s: Session) => !s.archived && (s.state === "waiting" || s.waiting > 0);

/** Before the feed's first read: the cards the sessions wait on. */
function cards(sessions: Record<string, Session>): number {
  let n = 0;
  for (const s of Object.values(sessions)) if (!s.archived) n += s.waiting > 0 ? s.waiting : s.state === "waiting" ? 1 : 0;
  return n;
}

interface LiveLike {
  subscribe: (listener: () => void) => () => void;
  getState: () => State;
}

/** The one count of what waits ({@link todayCount}); until the feed is in, the cards the sessions wait on. */
export function useTodayCount(source: FeedSource = feedSource, live: LiveLike = theLive): number {
  const subscribe = useCallback(
    (f: () => void) => {
      const a = source.subscribe(f);
      const b = live.subscribe(f);
      return () => {
        a();
        b();
      };
    },
    [source, live],
  );
  const count = () => {
    const feed = source.getFeed();
    return feed ? todayCount(feed) : cards(live.getState().sessions);
  };
  return useSyncExternalStore(subscribe, count, count);
}

const REQUESTS = new Set(["answer", "decide", "reply", "read", "step"]);

/** The sessions' requests — a question, a card to decide, a message awaited, a step of his own, a finished turn to
 * look at — in the feed's order; with `project`, its and its sub-projects' only. Her world and the dates are not
 * among them. */
export function requests(feed: Feed | null, project?: { path: string; of: ProjectOf }): FeedRow[] {
  const all = rowsOf(feed).filter((r) => !!r.session && REQUESTS.has(r.action));
  return project ? all.filter((r) => within(project.of(r), project.path)) : all;
}

/** The requests that wait, by key: what the operator's numbers are given to. */
export const waitingKeys = (feed: Feed | null): string[] => requests(feed).filter(waits).map((r) => r.key);

/** What waits in a project and its sub-projects: its requests that wait — before the feed is in, its sessions that
 * wait. "" (none) counts nothing. */
export function waitingIn(feed: Feed | null, sessions: Session[], of: ProjectOf): (project: string) => number {
  const rows = requests(feed).filter(waits);
  const waiting = sessions.filter(sessionWaits);
  return (project) =>
    !project ? 0 : feed ? rows.filter((r) => within(of(r), project)).length : waiting.filter((s) => within(of(s), project)).length;
}
