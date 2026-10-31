// Comments on a diff's lines, gathered before they go: a tap on a line of any diff (a Base card's, a push's, a code
// session's «Changes») writes one; they wait in the session's tray — kept in this browser per session, so a closed diff
// or a reload loses none — and «Send» puts them all to the session as one message through its input, as if typed. The
// quoted line is data: text in the message, never rendered or run.
import { useSyncExternalStore } from "react";

export type Side = "additions" | "deletions";

export interface Remark {
  id: string;
  file: string;
  line: number;
  side: Side;
  /** The line as the diff showed it, one line, cut. */
  quote: string;
  text: string;
}

const PREFIX = "retinue.remarks.";
const QUOTE_CHARS = 300;
const EMPTY: Remark[] = [];

export function commentText(file: string, line: number, text: string): string {
  return `Comment on ${file}:${line}: ${text.trim()}`;
}

/** The line as quoted: its first line, without the diff's own marks of a wrap, cut. */
export function quoteOf(line: string): string {
  const one = (line.split(/\r?\n/)[0] ?? "").replace(/\s+$/, "");
  return one.length > QUOTE_CHARS ? `${one.slice(0, QUOTE_CHARS - 1)}…` : one;
}

/** The message the tray sends: each comment as `Comment on <file>:<line>: <text>` with its line quoted under it. */
export function remarksMessage(remarks: Remark[]): string {
  const head = remarks.length === 1 ? "A comment on the diff:" : `Comments on the diff (${remarks.length}):`;
  const parts = remarks.map((r) => {
    const said = commentText(r.file, r.line, r.side === "deletions" ? `(the removed line) ${r.text.trim()}` : r.text);
    return r.quote ? `${said}\n> ${r.quote}` : said;
  });
  return [head, ...parts].join("\n\n");
}

function storage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

function valid(r: unknown): r is Remark {
  if (!r || typeof r !== "object") return false;
  const x = r as Record<string, unknown>;
  return (
    typeof x.id === "string" &&
    typeof x.file === "string" &&
    typeof x.line === "number" &&
    (x.side === "additions" || x.side === "deletions") &&
    typeof x.quote === "string" &&
    typeof x.text === "string"
  );
}

/** The trays of every session: in memory, mirrored to localStorage when it is there (a private window may have none). */
export class Remarks {
  private cache = new Map<string, Remark[]>();
  private listeners = new Set<() => void>();

  constructor(private store: Storage | null = storage()) {}

  list = (session: string): Remark[] => {
    const known = this.cache.get(session);
    if (known) return known;
    let read: Remark[] = EMPTY;
    try {
      const raw = this.store?.getItem(PREFIX + session);
      const parsed: unknown = raw ? JSON.parse(raw) : null;
      if (Array.isArray(parsed)) read = parsed.filter(valid);
    } catch {
      // unreadable: an empty tray
    }
    this.cache.set(session, read);
    return read;
  };

  private put(session: string, remarks: Remark[]): void {
    this.cache.set(session, remarks.length ? remarks : EMPTY);
    try {
      if (remarks.length) this.store?.setItem(PREFIX + session, JSON.stringify(remarks));
      else this.store?.removeItem(PREFIX + session);
    } catch {
      // storage full or blocked: the tray lives for this load
    }
    for (const l of this.listeners) l();
  }

  /** A comment on a line; one already on that line takes the new text. */
  add(session: string, remark: Omit<Remark, "id">): void {
    const rest = this.list(session).filter((r) => !(r.file === remark.file && r.line === remark.line && r.side === remark.side));
    this.put(session, [...rest, { ...remark, id: crypto.randomUUID() }]);
  }

  remove(session: string, id: string): void {
    this.put(session, this.list(session).filter((r) => r.id !== id));
  }

  /** Sent: the tray empties. Only the comments that were sent go — one added meanwhile stays. */
  clear(session: string, sent: Remark[]): void {
    const gone = new Set(sent.map((r) => r.id));
    this.put(session, this.list(session).filter((r) => !gone.has(r.id)));
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
}

export const remarks = new Remarks();

export function useRemarks(session: string, from: Remarks = remarks): Remark[] {
  return useSyncExternalStore(from.subscribe, () => from.list(session));
}

export interface PlainLine {
  text: string;
  /** Where a comment on it points; absent on a line that is no file's (a commit's header, a hunk's). */
  file?: string;
  line?: number;
  side?: Side;
}

/** A diff as plain text, line by line, each line of a hunk with its file and number: the new side for an added or an
 * unchanged line, the old side for a removed one. A commit's header and message between files point nowhere. */
export function plainLines(text: string): PlainLine[] {
  let file: string | null = null;
  let hunk = false;
  let old = 0;
  let neu = 0;
  return text.split("\n").map((row) => {
    if (row.startsWith("diff --git ")) {
      file = / b\/(.+)$/.exec(row)?.[1] ?? null;
      hunk = false;
      return { text: row };
    }
    if (!hunk && row.startsWith("+++ ")) {
      const name = row.slice(4);
      if (name !== "/dev/null") file = name.replace(/^b\//, "");
      return { text: row };
    }
    const at = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(row);
    if (at) {
      old = Number(at[1]);
      neu = Number(at[2]);
      hunk = file !== null;
      return { text: row };
    }
    if (!hunk || !file) return { text: row };
    const mark = row[0];
    if (mark === "+") return { text: row, file, line: neu++, side: "additions" };
    if (mark === "-") return { text: row, file, line: old++, side: "deletions" };
    if (mark === " ") {
      old++;
      return { text: row, file, line: neu++, side: "additions" };
    }
    if (mark !== "\\") hunk = false; // the hunk is over: the next commit's header, or the end
    return { text: row };
  });
}
