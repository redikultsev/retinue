// «Waiting for you»: one queue by action across the sessions and the assistant's world, built by the platform on each read
// (`GET /api/feed`, retinue/feed.py). The app does not fold it from its own store — the store holds the cards and
// summaries only of the sessions it has opened — it reads the feed again whenever a session, a card or a summary
// changes on the stream (debounced), and once a minute for the assistant's digest, which comes on no stream.

import { useSyncExternalStore } from "react";
import { request } from "./api";
import type { State } from "./store";
import type { Approval } from "./types";

export type FeedAction = "answer" | "decide" | "reply" | "read" | "assistant";
/** Where a row came from: a card's kind, a session, or the kind of the assistant's item. */
export type FeedSourceKind =
  | "question"
  | "approval"
  | "courier"
  | "session"
  | "letter"
  | "chat"
  | "draft"
  | "deadline"
  | "base"
  | "push"
  | string;

export interface FeedRow {
  key: string;
  action: FeedAction;
  source: FeedSourceKind;
  /** The session it belongs to; null for the assistant's. */
  session: string | null;
  track: string | null;
  sphere: string | null;
  sphere_title: string | null;
  sphere_icon: string | null;
  /** A session's title, or who the assistant's item is about. */
  title: string;
  /** Since when it waits (seconds). */
  since: number;
  /** Its one line: the question, the command, the summary's first line, the assistant's title. */
  essence: string;
  ref: string;
  /** «Answer», «Decide»: the card itself, decided in place. */
  approval?: Approval;
  /** «Decide» · «To the Base»: the session's Base files not committed yet (retinue/tobase.py). */
  base?: number;
  /** «Decide» · «Push»: commits no branch of the remote has, and in which repositories (retinue/topush.py). */
  push?: number;
  repos?: string[];
  /** «From the assistant»: a person's row's token — what «Done», «Not a task», «Tomorrow» name it by. Absent on the
   * private matter's row, a draft and a date. */
  t?: string;
  /** «From the assistant»: the owner's private matter — its words and buttons wait behind «Show» in Telegram, and the
   * app never asks for its sheet. */
  private?: boolean;
}

export interface FeedGroup {
  action: FeedAction;
  title: string;
  rows: FeedRow[];
}

export interface Feed {
  schema: number;
  count: number;
  groups: FeedGroup[];
  /** The bot's username: the assistant's rows open its chat. Empty before the router's first push. */
  bot: string;
  /** When the router last pushed its digest (seconds), or null. */
  assistant_at: number | null;
  /** «Deadlines»: the base's dates of the next week (and the overdue), by day — from the router's digest. Absent from a
   * platform before them. */
  deadlines?: Deadline[];
  /** The hub's status chips, as the router last pushed them. */
  chips?: Chip[];
  /** Her asks closed today, and those that ran out with no mark in the week — the router's counts. */
  closed_today?: number;
  expired_week?: number;
}

/** Where a thing is: its Gmail thread, or the Telegram chat by the person's @username — the router's own shapes. */
export interface DetailLink {
  url: string;
  label: "Open in Gmail" | "Open the chat" | string;
}

/** One ask of a person's row: what she judged it asks, in her words (codes, numbers, links masked). */
export interface DetailAsk {
  need: "reply" | "decide" | "pay" | "attend" | "act" | string;
  text: string;
  deadline: string;
  at: number;
  open?: DetailLink;
}

/** A row's sheet (`POST /api/waiting/detail`): asked of the router when the row is opened, never kept by the platform. */
export interface WaitingDetail {
  kind: "letter" | "chat" | "draft";
  who: string;
  /** The owner's mailbox it came to (a letter, a draft by mail); "" otherwise. */
  account: string;
  /** Newest first, at most 10; `more` beyond them. */
  asks: DetailAsk[];
  more: number;
  open?: DetailLink;
  draft?: { text: string; channel: "mail" | "telegram"; subject: string; expires: number; state: "shown" | "hidden" | "link" };
}

/** The only links a sheet may draw — the platform's own check, made a second time. */
const LINK_SHAPES = [
  /^https:\/\/mail\.google\.com\/mail\/\?authuser=[^\s@/?#&]{1,64}@[^\s@/?#&]{1,190}#all\/[0-9a-f]{10,24}$/,
  /^https:\/\/t\.me\/[A-Za-z][A-Za-z0-9_]{3,31}$/,
];
export function linkOk(link: DetailLink | undefined): link is DetailLink {
  return !!link && LINK_SHAPES.some((shape) => shape.test(link.url));
}

/** A press on her row: «Done», «Not a task», «Tomorrow», and «Undo» after one. */
export type Press = "done" | "dismissed" | "later" | "open";

export interface Deadline {
  id: string;
  /** `YYYY-MM-DD` */
  when: string;
  /** The router's words: «in 3 days: Gym membership — valid until». */
  title: string;
  /** The Space (a sphere's name when it is one). */
  sphere: string | null;
  sphere_title: string | null;
  sphere_icon: string | null;
  /** The Record's path in the base, when it is a plain one: it opens. */
  path: string | null;
}

export interface Chip {
  key: "health" | "backup" | "limit" | "build" | string;
  text: string;
  level: "ok" | "warn" | "bad" | string;
}

export const DEBOUNCE_MS = 400;
export const POLL_MS = 60_000;
/** The router pushes at least hourly: a digest older than two hours is said to be old. */
export const STALE_DIGEST_S = 2 * 3600;
/** A row pressed here stays hidden while the feed still has it — the router's next digest takes a moment — but no
 * longer than this: a press that did not go through brings its row back. */
export const PRESSED_HIDE_S = 600;

interface LiveLike {
  subscribe: (listener: () => void) => () => void;
  getState: () => State;
}

export interface FeedDeps {
  load: () => Promise<Feed>;
  setTimer: (fn: () => void, ms: number) => ReturnType<typeof setTimeout>;
  clearTimer: (timer: ReturnType<typeof setTimeout>) => void;
  /** Milliseconds now; `Date.now` by default. */
  now?: () => number;
}

const defaultDeps: FeedDeps = {
  load: () => request<Feed>("/api/feed"),
  setTimer: (fn, ms) => setTimeout(fn, ms),
  clearTimer: (timer) => clearTimeout(timer),
};

/** The feed without the rows whose token was pressed here, its count down by as many. */
export function withoutPressed(feed: Feed, pressed: ReadonlySet<string>): Feed {
  let gone = 0;
  const groups = feed.groups
    .map((g) => {
      const rows = g.rows.filter((r) => !(r.t && pressed.has(r.t)));
      gone += g.rows.length - rows.length;
      return { ...g, rows };
    })
    .filter((g) => g.rows.length > 0);
  return gone ? { ...feed, count: feed.count - gone, groups } : feed;
}

export class FeedSource {
  feed: Feed | null = null;
  private raw: Feed | null = null;
  /** Tokens pressed here, and when (ms). */
  private pressed = new Map<string, number>();
  private listeners = new Set<() => void>();
  private unsubscribe: (() => void) | null = null;
  private debounce: ReturnType<typeof setTimeout> | null = null;
  private poll: ReturnType<typeof setTimeout> | null = null;
  private seen: Pick<State, "sessions" | "approvals" | "summaries"> | null = null;
  private loading = false;
  private again = false;

  constructor(private deps: FeedDeps = defaultDeps) {}

  getFeed = (): Feed | null => this.feed;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  /** Follow the live store: what changes a session, a card or a summary reads the feed again. */
  start(live: LiveLike): void {
    if (this.unsubscribe) return;
    this.unsubscribe = live.subscribe(() => {
      const { sessions, approvals, summaries } = live.getState();
      const seen = this.seen;
      this.seen = { sessions, approvals, summaries };
      if (seen && seen.sessions === sessions && seen.approvals === approvals && seen.summaries === summaries) return;
      this.soon();
    });
    void this.refresh();
  }

  stop(): void {
    this.unsubscribe?.();
    this.unsubscribe = null;
    for (const timer of [this.debounce, this.poll]) if (timer) this.deps.clearTimer(timer);
    this.debounce = this.poll = null;
    this.seen = null;
    this.feed = this.raw = null;
    this.pressed.clear();
    this.notify();
  }

  /** A row pressed: out of the feed until the feed has it no more, or PRESSED_HIDE_S. */
  hide(t: string): void {
    this.pressed.set(t, this.now());
    this.apply();
  }

  /** «Undo», or a press that failed: the row is back as the feed has it. */
  unhide(t: string): void {
    this.pressed.delete(t);
    this.apply();
  }

  private now(): number {
    return (this.deps.now ?? Date.now)();
  }

  private apply() {
    const now = this.now();
    const tokens = new Set((this.raw?.groups ?? []).flatMap((g) => g.rows.flatMap((r) => (r.t ? [r.t] : []))));
    for (const [t, at] of this.pressed) if (!tokens.has(t) || now - at > PRESSED_HIDE_S * 1000) this.pressed.delete(t);
    this.feed = this.raw && withoutPressed(this.raw, new Set(this.pressed.keys()));
    this.notify();
  }

  /** Read again shortly: a burst of events is one read. */
  soon(): void {
    if (this.debounce) this.deps.clearTimer(this.debounce);
    this.debounce = this.deps.setTimer(() => {
      this.debounce = null;
      void this.refresh();
    }, DEBOUNCE_MS);
  }

  /** Read it now; a read asked for while one runs follows it. A failed read keeps the last feed. */
  async refresh(): Promise<void> {
    if (this.loading) {
      this.again = true;
      return;
    }
    this.loading = true;
    try {
      this.raw = await this.deps.load();
      this.apply();
    } catch {
      // offline or logged out: the live store says so; the last feed stays on screen
    } finally {
      this.loading = false;
      if (this.poll) this.deps.clearTimer(this.poll);
      if (this.unsubscribe) this.poll = this.deps.setTimer(() => void this.refresh(), POLL_MS);
      if (this.again) {
        this.again = false;
        void this.refresh();
      }
    }
  }

  private notify() {
    for (const l of this.listeners) l();
  }
}

export const feedSource = new FeedSource();

export function useFeed(source: FeedSource = feedSource): Feed | null {
  return useSyncExternalStore(source.subscribe, source.getFeed);
}

/** How long a row waits: «5 min», «3 h», «2 d». */
export function waited(since: number, now: number = Date.now() / 1000): string {
  const minutes = Math.max(1, Math.round((now - since) / 60));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h`;
  return `${Math.round(hours / 24)} d`;
}
