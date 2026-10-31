// The app's live state: one store (the reducer in store.ts) fed by snapshots and ONE EventSource for every
// session on screen (E8: two panels, one connection).
//
// The stream resumes from the last event id. EventSource reconnects by itself after a network error (and sends
// Last-Event-ID); an HTTP error closes it for good, and the status is then probed with fetch: 410 means the cursor
// is older than the oldest kept event, so the snapshots are reloaded; 401 sends the owner to the login screen.
// iOS freezes a hidden PWA's connection without telling it: back after more than 45 s, the stream is reopened.

import { useSyncExternalStore } from "react";
import { ApiError, api } from "./api";
import { type Action, initialState, reduce, type State, waitsOnOwner } from "./store";
import { type Approval, EVENT_KINDS, type Meta, SCHEMA, type StreamEvent } from "./types";

export const HIDDEN_RECONNECT_MS = 45_000;
const RETRY_MS = [1_000, 2_000, 5_000, 10_000, 30_000];

export type Connection = "idle" | "connecting" | "live" | "offline" | "outdated";

export interface LiveDeps {
  api: Pick<typeof api, "meta" | "sessions" | "session">;
  openSource: (url: string) => EventSource;
  probe: (url: string) => Promise<number>;
  now: () => number;
  reload: () => void;
  onUnauthorized: () => void;
}

async function probeStatus(url: string): Promise<number> {
  const abort = new AbortController();
  try {
    const res = await fetch(url, { credentials: "same-origin", signal: abort.signal });
    return res.status;
  } catch {
    return 0;
  } finally {
    abort.abort();
  }
}

export class Live {
  private state: State = initialState;
  private listeners = new Set<() => void>();
  private cardListeners = new Set<(approval: Approval) => void>();
  private source: EventSource | null = null;
  private watched = new Set<string>();
  private hiddenAt: number | null = null;
  private retry = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private started = false;
  /** The app wants the stream (start() called, stop() not): a failed start is retried. */
  private wanted = false;
  meta: Meta | null = null;
  connection: Connection = "idle";

  constructor(private deps: LiveDeps) {}

  // --- the store --------------------------------------------------------------------------------------------------

  getState = (): State => this.state;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  dispatch = (action: Action): void => {
    this.state = reduce(this.state, action);
    for (const l of this.listeners) l();
  };

  private setConnection(c: Connection) {
    if (this.connection === c) return;
    this.connection = c;
    this.state = { ...this.state }; // a new reference so subscribers re-read the connection
    for (const l of this.listeners) l();
  }

  // --- snapshots --------------------------------------------------------------------------------------------------

  private schemaOk(schema: number): boolean {
    if (schema === SCHEMA) return true;
    // The server speaks another schema: this build is outdated. A reload fetches the new app shell and snapshot.
    this.setConnection("outdated");
    this.deps.reload();
    return false;
  }

  /** Meta, the session list, the snapshots of sessions with waiting cards; then the stream. */
  async start(): Promise<void> {
    if (this.started) return;
    this.started = true;
    this.wanted = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.setConnection("connecting");
    try {
      const [meta, list] = await Promise.all([this.deps.api.meta(), this.deps.api.sessions()]);
      if (!this.schemaOk(meta.schema) || !this.schemaOk(list.schema)) return;
      this.meta = meta;
      this.dispatch({ type: "list", snapshot: list });
      this.open();
      this.retry = 0;
      await Promise.all(list.sessions.filter((s) => s.waiting > 0).map((s) => this.watch(s.id).catch(() => undefined)));
    } catch (e) {
      this.started = false;
      this.fail(e);
      if (e instanceof ApiError && e.status === 401) return;
      // No server yet (a restart, no network): try again later; coming back to the tab tries at once.
      const wait = RETRY_MS[Math.min(this.retry++, RETRY_MS.length - 1)] ?? 30_000;
      this.timer = setTimeout(() => {
        this.timer = null;
        if (this.wanted) void this.start();
      }, wait);
    }
  }

  stop(): void {
    this.started = false;
    this.wanted = false;
    this.close();
    this.watched.clear();
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.state = initialState;
    this.setConnection("idle");
  }

  /** Load a session's snapshot (its items and cards) and keep it fresh across resyncs. */
  async watch(id: string): Promise<void> {
    this.watched.add(id);
    try {
      const snapshot = await this.deps.api.session(id);
      if (this.schemaOk(snapshot.schema)) this.dispatch({ type: "session", snapshot });
    } catch (e) {
      this.fail(e);
      throw e;
    }
  }

  /** After 410: every snapshot again, and the stream from the new cursor. */
  async resync(): Promise<void> {
    this.close();
    const list = await this.deps.api.sessions();
    if (!this.schemaOk(list.schema)) return;
    const snapshots = await Promise.all([...this.watched].map((id) => this.deps.api.session(id).catch(() => null)));
    this.state = { ...initialState, deltas: {} };
    this.dispatch({ type: "list", snapshot: list });
    for (const s of snapshots) if (s && this.schemaOk(s.schema)) this.dispatch({ type: "session", snapshot: s });
    this.open();
  }

  private fail(e: unknown) {
    if (e instanceof ApiError && e.status === 401) {
      this.deps.onUnauthorized();
      return;
    }
    this.setConnection("offline");
  }

  // --- the stream -------------------------------------------------------------------------------------------------

  private url(): string {
    return `/api/events?since=${this.state.cursor ?? 0}`;
  }

  private open(): void {
    this.close();
    this.setConnection("connecting");
    const source = this.deps.openSource(this.url());
    this.source = source;
    source.onopen = () => {
      this.retry = 0;
      this.setConnection("live");
    };
    for (const kind of EVENT_KINDS) {
      source.addEventListener(kind, (e) => this.onEvent(kind, e as MessageEvent<string>));
    }
    source.addEventListener("delta", (e) => {
      const data = JSON.parse((e as MessageEvent<string>).data) as { session: string; text: string };
      this.dispatch({ type: "delta", session: data.session, text: data.text });
    });
    source.onerror = () => {
      if (this.source !== source) return;
      if (source.readyState === 2 /* CLOSED */) void this.recover();
      else this.setConnection("connecting"); // the browser retries by itself
    };
  }

  /** Told of each card that starts to wait for the owner while the app is open: a stream event that moves it to
   * «shown». A snapshot's cards (there before the app looked) are never told. */
  onNewCard(listener: (approval: Approval) => void): () => void {
    this.cardListeners.add(listener);
    return () => this.cardListeners.delete(listener);
  }

  private onEvent(kind: StreamEvent["kind"], e: MessageEvent<string>) {
    const id = e.lastEventId ? Number(e.lastEventId) : null;
    const event = { kind, data: JSON.parse(e.data) } as StreamEvent;
    const before = event.kind === "approval" ? this.state.approvals[event.data.id]?.state : undefined;
    this.dispatch({ type: "event", id: Number.isFinite(id) ? id : null, event });
    if (
      event.kind === "approval" &&
      waitsOnOwner(event.data) &&
      before !== "shown" &&
      this.state.approvals[event.data.id]?.state === "shown"
    ) {
      for (const l of this.cardListeners) l(event.data);
    }
    // A session that starts waiting: load its snapshot so its card is in «Waiting for you» with the full input.
    if (event.kind === "session" && event.data.waiting > 0 && !this.watched.has(event.data.id)) {
      this.watch(event.data.id).catch(() => undefined);
    }
  }

  isWatched(id: string): boolean {
    return this.watched.has(id);
  }

  private close(): void {
    this.source?.close();
    this.source = null;
  }

  private async recover(): Promise<void> {
    this.close();
    const status = await this.deps.probe(this.url());
    if (!this.started) return;
    if (status === 410) {
      await this.resync().catch((e) => this.fail(e));
      return;
    }
    if (status === 401) {
      this.deps.onUnauthorized();
      return;
    }
    this.setConnection("offline");
    const wait = RETRY_MS[Math.min(this.retry++, RETRY_MS.length - 1)] ?? 30_000;
    this.timer = setTimeout(() => this.started && this.open(), wait);
  }

  /** document.visibilitychange: reopen the stream when coming back after more than 45 s. */
  visibility(hidden: boolean): void {
    if (hidden) {
      this.hiddenAt = this.deps.now();
      return;
    }
    const away = this.hiddenAt === null ? 0 : this.deps.now() - this.hiddenAt;
    this.hiddenAt = null;
    if (this.wanted && !this.started) {
      void this.start(); // the first start failed and waits for its retry: try now
      return;
    }
    if (this.started && (away > HIDDEN_RECONNECT_MS || !this.source)) this.open();
  }
}

export const live = new Live({
  api,
  openSource: (url) => new EventSource(url, { withCredentials: true }),
  probe: probeStatus,
  now: () => Date.now(),
  reload: () => {
    // Once a minute at most, so a server on another schema cannot put the page in a reload loop.
    try {
      const last = Number(sessionStorage.getItem("retinue-schema-reload") ?? 0);
      if (Date.now() - last < 60_000) return;
      sessionStorage.setItem("retinue-schema-reload", String(Date.now()));
    } catch {
      // storage unavailable: reload anyway
    }
    window.location.reload();
  },
  onUnauthorized: () => {
    live.stop();
    const next = window.location.pathname + window.location.search;
    if (!window.location.pathname.startsWith("/login")) {
      // A Mini App (the bot's menu button, a card's button) carries Telegram's signed data in the hash: it goes along,
      // so the login page logs in by it instead of asking for a code.
      const hash = window.location.hash.includes("tgWebAppData=") ? window.location.hash : "";
      window.location.assign(`/login?next=${encodeURIComponent(next)}${hash}`);
    }
  },
});

/** The live state; the component re-renders on every change. Derive with useMemo keyed on it. */
export function useLiveState(source: Live = live): State {
  return useSyncExternalStore(source.subscribe, source.getState);
}

export function useConnection(source: Live = live): Connection {
  return useSyncExternalStore(source.subscribe, () => source.connection);
}
