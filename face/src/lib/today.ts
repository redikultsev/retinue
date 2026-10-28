// «Сегодня»: what the home screen folds from what the app already has — the session rows (sphere, state, unread), the
// stream's items and the text being written, the feed's deadlines. Nothing here asks the server anything.
import { useSyncExternalStore } from "react";
import { plural } from "./format";
import type { Item, Session } from "./types";

const isWaiting = (s: Session) => s.state === "waiting" || s.waiting > 0;

/** When a running session's turn began: the platform's `turn_started`, else (an older platform) its row's `at`. */
export const turnStart = (s: Session) => s.turn_started ?? s.at;

/** «Работают»: the sessions running a turn now (not those waiting on the owner), the longest running first. */
export function running(sessions: Session[]): Session[] {
  return sessions.filter((s) => s.state === "active" && !isWaiting(s)).sort((a, b) => turnStart(a) - turnStart(b));
}

/** How often «Работают» ticks: every second while a turn is under a minute old, else every 30 s. */
export function tickMs(sessions: Session[], now: number): number {
  return sessions.some((s) => now - turnStart(s) < 60) ? 1000 : 30_000;
}

/** «Готово»: the sessions that finished since `since` and that the owner has not read yet, newest first. A session
 * still running or waiting on him is not done. */
export function finished(sessions: Session[], since: number): Session[] {
  return sessions
    .filter((s) => s.unread && s.state !== "active" && !isWaiting(s) && s.at >= since)
    .sort((a, b) => b.at - a.at);
}

const LINE_CHARS = 140;

function cut(text: string): string {
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  // markdown's marks at the start go; a table row reads as its cells
  const last = (lines.at(-1) ?? "").replace(/^[#>*\-|\s]+/, "").replace(/\s*\|\s*$/, "").replace(/\s*\|\s*/g, " · ");
  return last.length > LINE_CHARS ? `${last.slice(0, LINE_CHARS - 1)}…` : last;
}

function itemLine(item: Item): string {
  switch (item.kind) {
    case "message":
      return item.data.role === "assistant" ? cut(item.data.text) : "";
    case "tool": {
      const input = item.data.input;
      const what = ["command", "description", "file_path", "url", "query", "pattern", "path"]
        .map((k) => input[k])
        .find((v): v is string => typeof v === "string" && v.trim() !== "");
      return cut(what ? `${item.data.tool}: ${what}` : item.data.tool);
    }
    case "reads":
      return `читает: ${item.data.calls.length} ${plural(item.data.calls.length, "файл", "файла", "файлов")}`;
    case "edit":
      return cut(`правит ${item.data.path}`);
    case "subagent":
      return cut(`субагент: ${item.data.description || item.data.agent}`);
    case "plan":
      return item.data.tool === "TodoWrite" ? "обновляет план" : "предлагает план";
    case "question":
      return "задаёт вопрос";
    case "result":
      return "";
  }
}

/** A running session's last line: the text being written now, else the newest item that says something. */
export function lastLine(items: Item[], delta: string | undefined): string {
  if (delta?.trim()) return cut(delta);
  for (let k = items.length - 1; k >= 0; k--) {
    const line = itemLine(items[k]!);
    if (line) return line;
  }
  return "";
}

/** How long a turn has run: «40 с», «12 мин», «1 ч 05 мин». */
export function elapsed(since: number, now: number): string {
  const s = Math.max(0, Math.round(now - since));
  if (s < 60) return `${s} с`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} мин`;
  return `${Math.floor(m / 60)} ч ${String(m % 60).padStart(2, "0")} мин`;
}

const WEEKDAYS = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"];
const MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];

/** «вт 28 окт» */
export function todayWord(now: Date): string {
  return `${WEEKDAYS[now.getDay()]} ${now.getDate()} ${MONTHS[now.getMonth()]}`;
}

/** A deadline's day (`YYYY-MM-DD`) against today, in the owner's words: «пт 31.10» and «через 3 дня». */
export function dayWords(when: string, now: Date): { date: string; rel: string; overdue: boolean } {
  const [y, m, d] = when.split("-").map(Number) as [number, number, number];
  const day = new Date(y, m - 1, d);
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const days = Math.round((day.getTime() - today.getTime()) / 86_400_000);
  const date = `${WEEKDAYS[day.getDay()]} ${String(d).padStart(2, "0")}.${String(m).padStart(2, "0")}`;
  const rel =
    days === 0
      ? "сегодня"
      : days === 1
        ? "завтра"
        : days > 1
          ? `через ${days} ${plural(days, "день", "дня", "дней")}`
          : `просрочено ${-days} ${plural(-days, "день", "дня", "дней")}`;
  return { date, rel, overdue: days < 0 };
}

// --- since when «Готово» counts ----------------------------------------------------------------------------------

export const SEEN_KEY = "retinue-app-seen";
/** First run on this device: «Готово» looks back a day. */
const FIRST_LOOK_S = 86_400;
/** Away for less than this is the same visit. */
const SAME_VISIT_S = 60;

/** When the owner last had the app in front of him, kept on the device: where «Готово» starts. Read once when the
 * app loads; while it runs, coming back after more than a minute away moves it to when he left. */
export class Visits {
  private since: number;
  private hiddenAt: number | null = null;
  private listeners = new Set<() => void>();

  constructor(
    private storage: Pick<Storage, "getItem" | "setItem"> | null,
    private now: () => number = () => Date.now() / 1000,
  ) {
    const kept = Number(this.read() ?? Number.NaN);
    this.since = Number.isFinite(kept) && kept > 0 ? kept : this.now() - FIRST_LOOK_S;
  }

  private read(): string | null {
    try {
      return this.storage?.getItem(SEEN_KEY) ?? null;
    } catch {
      return null;
    }
  }

  getSince = (): number => this.since;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  /** The app went to the background (or is closing): this is when he last saw it. */
  hidden(): void {
    this.hiddenAt = this.now();
    try {
      this.storage?.setItem(SEEN_KEY, String(Math.floor(this.hiddenAt)));
    } catch {
      // storage unavailable: «Готово» counts from this load
    }
  }

  shown(): void {
    if (this.hiddenAt !== null && this.now() - this.hiddenAt > SAME_VISIT_S) {
      this.since = this.hiddenAt;
      for (const l of this.listeners) l();
    }
    this.hiddenAt = null;
  }
}

function storage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

export const visits = new Visits(storage());

export function useSince(source: Visits = visits): number {
  return useSyncExternalStore(source.subscribe, source.getSince);
}
