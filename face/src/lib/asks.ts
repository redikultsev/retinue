// «From people» (docs/face.md «Tasks and Schedule»): a person's ask — a letter's or a chat's row of the assistant's
// digest, with the token the router names it by — is Tasks' to show, not «Today»'s. Which rows are asks, which of them
// no open task names yet, and a press on one: «Done», «Not a task» or «Tomorrow» takes it off at once — Tasks, its
// count and its sheet alike —, the router marks what the token names, and the toast's «Undo» brings it back. The asks
// come from the feed (`/api/feed`), the one read that holds a press; the plan only says which a task names.
import { useSyncExternalStore } from "react";
import { api } from "./api";
import { type Feed, type FeedRow, type FeedSource, type Press, feedSource } from "./feed";
import { named, type Plan } from "./plan";
import { toast } from "./toast";

/** A row pressed here stays hidden while the feed still has it — the router's next digest takes a moment — but no
 * longer than this: a press that did not go through brings its row back. */
export const PRESSED_HIDE_S = 600;
const UNDO_MS = 10_000;
const PRESS_FAILED = "It did not go through — try again.";

/** A person's ask: a row with its token (a draft and a date have none). */
export const isAsk = (row: FeedRow): boolean => !!row.t && row.source !== "draft";

const asksIn = (feed: Feed | null) => (feed?.groups ?? []).flatMap((g) => g.rows).filter(isAsk);

// The tokens pressed here, and when (ms); a new map on every change, so a screen reads it as a snapshot.
let pressed: ReadonlyMap<string, number> = new Map();
const subs = new Set<() => void>();
const subscribe = (f: () => void) => {
  subs.add(f);
  return () => subs.delete(f);
};
function put(next: Map<string, number>) {
  pressed = next;
  for (const f of subs) f();
}
const hide = (t: string, now: number) => put(new Map([...pressed, [t, now]]));
function unhide(t: string) {
  if (!pressed.has(t)) return;
  const next = new Map(pressed);
  next.delete(t);
  put(next);
}

// A press is forgotten once the feed has its row no more: a row that comes back later (an «Undo» in Telegram) shows.
let following: (() => void) | null = null;
function follow(source: FeedSource) {
  following ??= source.subscribe(() => {
    const feed = source.getFeed();
    if (!feed) return;
    const there = new Set(asksIn(feed).map((r) => r.t));
    const next = new Map([...pressed].filter(([t]) => there.has(t)));
    if (next.size !== pressed.size) put(next);
  });
}

/** For tests: forget the presses. */
export function forgetPressed(): void {
  following?.();
  following = null;
  put(new Map());
}

/** The asks «From people» lists, in the feed's order: none pressed here (for ten minutes at most), none an open task
 * already names (its `ask`); none at all until the plan is read, which says what the tasks name. */
export function asksOf(feed: Feed | null, plan: Plan | null, held: ReadonlyMap<string, number> = pressed, now = Date.now()): FeedRow[] {
  if (!plan) return [];
  const taken = named(plan);
  const off = (t: string) => {
    const at = held.get(t);
    return at !== undefined && now - at <= PRESSED_HIDE_S * 1000;
  };
  return asksIn(feed).filter((r) => !off(r.t as string) && !taken.has(r.ref));
}

/** {@link asksOf}, kept up as asks are pressed and taken back. */
export function useAsks(feed: Feed | null, plan: Plan | null): FeedRow[] {
  const held = useSyncExternalStore(subscribe, () => pressed, () => pressed);
  return asksOf(feed, plan, held);
}

/** «Done», «Not a task», «Tomorrow» on an ask: off the list at once, the router marks what its token names, and the
 * toast offers «Undo» (`open` with the same token) for ten seconds. False when the press did not go through. */
export async function pressAsk(
  t: string,
  action: Press,
  label: string,
  { press = api.waitingPress, source = feedSource, now = Date.now }: { press?: typeof api.waitingPress; source?: FeedSource; now?: () => number } = {},
): Promise<boolean> {
  follow(source);
  const back = async () => {
    try {
      await press(t, "open");
      unhide(t);
    } catch {
      toast(PRESS_FAILED, "error");
    }
  };
  hide(t, now());
  try {
    await press(t, action);
  } catch {
    unhide(t);
    toast(PRESS_FAILED, "error");
    return false;
  }
  toast(label, "info", UNDO_MS, undefined, { label: "Undo", run: () => void back() });
  return true;
}
