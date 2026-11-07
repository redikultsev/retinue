// «Settings» (docs/face.md «Settings», retinue/settings.py): what the stack reads, as the server reports it — whether
// each key is set and when Settings changed it, never a value — and the owner's changes, each a request root writes
// on the host. The value he types is sent once and dropped from the page's state at once.
import { useCallback, useEffect, useRef, useState } from "react";
import { request } from "./api";
import { paths } from "./contract";

export type SettingsKey = {
  name: string;
  group: "keys" | "channels";
  title: string;
  what: string;
  where: string;
  hint: string;
  /** It may be removed: an optional part's. */
  clear: boolean;
  /** Its part runs here (a key of a part turned off is kept for when it is on). */
  on: boolean;
  set: boolean;
  /** When Settings last changed it (seconds); null: set by hand or never. */
  changed: number | null;
};
/** An account's token: the repositories in use with it, and those added to platform.yaml, in use after the restart. */
export type GitToken = { key: string; repos: string[]; added: string[]; set: boolean; changed: number | null };
export type ProjectKey = { name: string; project: string; set: boolean; changed: number | null };
export type Lane = {
  name: string;
  engine: string;
  engine_label: string;
  /** What platform.yaml says now; `running`: what the platform runs with until its restart. */
  model: string;
  effort: string;
  running: { model: string; effort: string };
};
export type Choice = { id: string; label: string };
/** A computer of /mcp/device: what it may do, when it was last seen (seconds), whether it was taken back. */
export type Device = {
  name: string;
  scope: "read" | "write";
  created: number;
  seen: number | null;
  revoked: number | null;
};
export type Done = { ok: boolean; at: number; why?: string; services?: string[] };
export type SettingsView = {
  schema: number;
  /** The host's settings service has written its status: without it nothing can be changed. */
  ready: boolean;
  at: number | null;
  /** The services a saved change waits on. */
  pending: string[];
  restarting: string[];
  restarted: number | null;
  lanes: Lane[];
  models: Choice[];
  efforts: Choice[];
  keys: SettingsKey[];
  git: GitToken[];
  project_keys: ProjectKey[];
  /** The first repository makes the `code:` section, and its commits need an author: none in platform.yaml yet. */
  author_needed: boolean;
  done: Record<string, Done>;
  /** null: the platform could not read them just now. */
  devices: Device[] | null;
};
export type SettingsOp = "key" | "git" | "project_key" | "lane" | "repo" | "restart";

export const settingsApi = {
  get: () => request<SettingsView>(paths.settings),
  send: (op: SettingsOp, body: Record<string, unknown>) => request<{ id: string }>(paths.settingsDo(op), body),
  revoke: (name: string) => request<{ revoked: boolean }>(paths.settingsRevoke, { name }),
};

/** What a service is called on screen. */
export const SERVICE_LABELS: Record<string, string> = {
  router: "Telegram",
  assistant: "the assistant",
  platform: "the sessions and this app",
  "tg-business": "Telegram Business",
};

const POLL_MS = 1500;
const POLL_LIMIT_MS = 5 * 60_000; // a restart of every service, images pulled again

/**
 * The screen's state: the view, and the requests sent and not yet answered by the host (`waiting`, by id → what
 * they are about). While one waits the view is asked again every 1.5 s; a call that fails meanwhile (the platform
 * restarting) is tried again until the host has answered or five minutes have passed.
 */
export function useSettings() {
  const [view, setView] = useState<SettingsView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [waiting, setWaiting] = useState<Record<string, { about: string; since: number }>>({});
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await settingsApi.get();
      setView(next);
      setError(null);
      setWaiting((w) => {
        const left = Object.fromEntries(
          Object.entries(w).filter(([id, { since }]) => !next.done[id] && Date.now() - since < POLL_LIMIT_MS),
        );
        return Object.keys(left).length === Object.keys(w).length ? w : left;
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "The server did not answer.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const busy = Object.keys(waiting).length > 0;
  useEffect(() => {
    if (!busy) return;
    timer.current = setTimeout(() => void load(), POLL_MS);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  });

  /** Send one change; the id comes back at once, the host's answer with a later view. */
  const send = useCallback(async (op: SettingsOp, about: string, body: Record<string, unknown>) => {
    const { id } = await settingsApi.send(op, body);
    setWaiting((w) => ({ ...w, [id]: { about, since: Date.now() } }));
    return id;
  }, []);

  /** A device taken back by the platform itself, at once: the view is read again after it. */
  const revoke = useCallback(
    async (name: string) => {
      await settingsApi.revoke(name);
      await load();
    },
    [load],
  );

  return { view, error, waiting, busy, load, send, revoke };
}

/** A request's state for the row that sent it: still with the host, answered, or none sent. */
export function requestState(
  id: string | null,
  waiting: Record<string, unknown>,
  view: SettingsView | null,
): "none" | "waiting" | "done" | "refused" | "lost" {
  if (!id) return "none";
  if (id in waiting) return "waiting";
  const done = view?.done[id];
  if (!done) return "lost"; // five minutes without an answer
  return done.ok ? "done" : "refused";
}
