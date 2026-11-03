// Talking to Retinue (docs/face.md «Talk to Retinue»): the chat beside every screen — the voice's main session
// (`meta.voice`, retinue/voice.py) —, open or closed as this device left it; and what the header's microphone does with
// a phrase: «1 — the recommended one, 2 — yes» answers the operator's cards by their numbers, anything else goes to
// the chat. Only the owner's own words are read here — what he typed or dictated —, never a model's.
import { useSyncExternalStore } from "react";
import type { FeedRow } from "./feed";
import type { Numbers } from "./operator";
import type { Session } from "./types";

// --- the chat, open or closed ----------------------------------------------------------------------------------

export const CHAT_KEY = "retinue-chat-open";
const subs = new Set<() => void>();
let open = read();
/** Words to start the chat's box with (the header's microphone heard no numbers): taken once by the box. */
let draft: { text: string; n: number } = { text: "", n: 0 };

function read(): boolean {
  try {
    return localStorage.getItem(CHAT_KEY) === "1";
  } catch {
    return false;
  }
}

function tell() {
  for (const f of subs) f();
}

export function setChatOpen(next: boolean): void {
  open = next;
  try {
    localStorage.setItem(CHAT_KEY, next ? "1" : "0");
  } catch {
    // storage unavailable: it stays as it is while the app is open
  }
  tell();
}

/** Opens the chat with these words in its box, to be read and sent by the owner. */
export function chatWith(text: string): void {
  draft = { text, n: draft.n + 1 };
  setChatOpen(true);
}

const subscribe = (f: () => void) => {
  subs.add(f);
  return () => subs.delete(f);
};

export function useChatOpen(): boolean {
  return useSyncExternalStore(subscribe, () => open, () => open);
}

export function useChatDraft(): { text: string; n: number } {
  return useSyncExternalStore(subscribe, () => draft, () => draft);
}

/** For tests: back to closed, nothing in the box. */
export function forgetChat(): void {
  open = false;
  draft = { text: "", n: 0 };
  tell();
}

/** The voice's main session: its sphere's main one, when the app lists it. */
export function voiceSession(sessions: Session[], sphere: string | null | undefined): Session | undefined {
  return sphere ? sessions.find((s) => s.sphere === sphere && s.main && !s.archived) : undefined;
}

// --- «1 — yes, 2 — the second one» ------------------------------------------------------------------------------

/** A number that addresses a card: one or two digits, then a dash — an em or en dash, spaces or not; a hyphen only with
 * a space either side, so «3-4 days» is no address. */
const ADDRESS = /(\d{1,2})(?:\s*[—–]\s*|\s+-\s+)/g;
/** What may stand before the next address: a comma, a semicolon, a full stop, a new line. */
const BEFORE = /(?:^|[,;.\n])\s*$/;

export interface Addressed {
  n: number;
  words: string;
}

/** The answers a phrase gives by number, when it starts with one («1 — the recommended one, 2 — yes»); none when it
 * does not: then it is a message, not answers. An address is taken only at the start or after a comma, a semicolon,
 * a full stop or a new line, so a number inside an answer stays in it. */
export function parseAnswers(text: string): Addressed[] {
  const found: { n: number; at: number; end: number }[] = [];
  for (const m of text.matchAll(ADDRESS)) {
    const at = m.index ?? 0;
    if (!BEFORE.test(text.slice(0, at))) continue;
    found.push({ n: Number(m[1]), at, end: at + m[0].length });
  }
  if (!found.length || text.slice(0, found[0]!.at).trim()) return [];
  return found
    .map((f, i) => ({ n: f.n, words: text.slice(f.end, found[i + 1]?.at ?? text.length).trim().replace(/[,;.]+$/, "").trim() }))
    .filter((a) => a.n > 0 && a.words);
}

export type Reach =
  | { n: number; words: string; row: FeedRow; how: "send" }
  /** A card of the platform's own — a question, an approval, a proposal, a push — is decided by its buttons. */
  | { n: number; words: string; row: FeedRow; how: "card" }
  | { n: number; words: string; row: null; how: "none" };

/** Each answer by the card its number stands for now (`numbers`, the operator's): one to send to its session, one
 * that is decided on its own card, one no card has. */
export function reach(answers: Addressed[], numbers: Numbers, rows: FeedRow[]): Reach[] {
  const byNumber = new Map(Object.entries(numbers).filter(([, x]) => x.freed === undefined).map(([key, x]) => [x.n, key]));
  return answers.map(({ n, words }) => {
    const row = rows.find((r) => r.key === byNumber.get(n));
    if (!row?.session) return { n, words, row: null, how: "none" };
    const card = !!(row.approval || row.proposal || row.collection_proposal || row.source === "push");
    return { n, words, row, how: card ? "card" : "send" };
  });
}
