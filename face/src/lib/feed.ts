// «Ждут тебя»: one queue by action across the sessions and the assistant's world, built by the platform on each read
// (`GET /api/feed`, retinue/feed.py). The app does not fold it from its own store — the store holds the cards and
// summaries only of the sessions it has opened — it reads the feed again whenever a session, a card or a summary
// changes on the stream (debounced), and once a minute for the assistant's digest, which comes on no stream.

import { useSyncExternalStore } from "react";
import { request } from "./api";
import type { State } from "./store";
import type { Approval } from "./types";

export type FeedAction = "answer" | "decide" | "choose" | "read" | "assistant";
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
  /** «Ответить на вопрос», «Решить»: the card itself, decided in place. */
  approval?: Approval;
  /** «Выбрать»: the turn's suggested replies (may be empty). */
  replies?: string[];
  summary?: number | null;
  /** «Решить» · «В Базу»: the session's Base files not committed yet (retinue/tobase.py). */
  base?: number;
  /** «Решить» · «Запушить»: commits no branch of the remote has, and in which repositories (retinue/topush.py). */
  push?: number;
  repos?: string[];
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
  /** «Сроки»: the base's dates of the next week (and the overdue), by day — from the router's digest. Absent from a
   * platform before them. */
  deadlines?: Deadline[];
  /** The hub's status chips, as the router last pushed them. */
  chips?: Chip[];
}

export interface Deadline {
  id: string;
  /** `YYYY-MM-DD` */
  when: string;
  /** The router's words: «через 3 дня: Абонемент в спортзал — действует до». */
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

interface LiveLike {
  subscribe: (listener: () => void) => () => void;
  getState: () => State;
}

export interface FeedDeps {
  load: () => Promise<Feed>;
  setTimer: (fn: () => void, ms: number) => ReturnType<typeof setTimeout>;
  clearTimer: (timer: ReturnType<typeof setTimeout>) => void;
}

const defaultDeps: FeedDeps = {
  load: () => request<Feed>("/api/feed"),
  setTimer: (fn, ms) => setTimeout(fn, ms),
  clearTimer: (timer) => clearTimeout(timer),
};

export class FeedSource {
  feed: Feed | null = null;
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
    this.feed = null;
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
      this.feed = await this.deps.load();
      this.notify();
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

/** How long a row waits: «5 мин», «3 ч», «2 дн». */
export function waited(since: number, now: number = Date.now() / 1000): string {
  const minutes = Math.max(1, Math.round((now - since) / 60));
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} ч`;
  return `${Math.round(hours / 24)} дн`;
}
