// «Today» as the operator works it (components/operator.tsx): what each card is, from the session's own word on how
// its turn ended (`ending` on the feed's row, docs/face.md «The operator»); the short numbers of the cards that wait;
// and an order that holds while the owner reads.
import { useRef, useSyncExternalStore } from "react";
import type { EndingArtifact, FeedRow, TurnEnding } from "./feed";

/** The ending a card is drawn by: none on a row that carries a card of its own (a question, an approval, a proposal, a
 * push) — that card is the thing decided. */
export function endingOf(row: FeedRow): TurnEnding | null {
  return row.approval || row.proposal || row.collection_proposal || row.source === "push" ? null : (row.ending ?? null);
}

const VERB: Record<string, string> = {
  ask: "asks",
  approve: "asks to allow",
  look: "asks you to look",
  step: "has a step for you",
  done: "is done",
  answer: "asks",
  decide: "waits for your decision",
  reply: "waits for your message",
  read: "is done",
};

/** The verb on a card's top line: by its ending, else by its row's action. */
export const verbOf = (row: FeedRow): string => VERB[endingOf(row)?.kind ?? row.action] ?? "waits";

/** A card that waits for the owner — a thing to look at too; a finished turn is only to read. */
export function waits(row: FeedRow): boolean {
  const e = endingOf(row);
  return e ? e.kind !== "done" : row.action !== "read";
}

export const WHY_OWNER: Record<string, string> = {
  goal: "your goal or taste",
  fact: "a fact only you know",
  money: "money",
  secret: "a secret or root",
  external: "irreversible, outside",
};

/** The things a card shows: a look's own, then those attached to any kind. */
export function thingsOf(e: TurnEnding | null | undefined): EndingArtifact[] {
  if (!e) return [];
  return [e.artifact, ...(e.artifacts ?? [])].filter((t): t is EndingArtifact => !!t);
}

export const thingName = (t: EndingArtifact) => t.title || (t.path ?? t.url ?? "").split(/[\\/]/).pop() || "file";

// --- numbers -----------------------------------------------------------------------------------------------------

/** A number freed by a card that left stays its own this long: «three» does not turn into another session while he
 * may still say it. */
export const HOLD_S = 30 * 60;
export type Numbers = Record<string, { n: number; freed?: number }>;

/** The waiting cards' numbers: each keeps its own; a new one takes the smallest number not taken or held. */
export function renumber(prev: Numbers, waiting: string[], now: number): Numbers {
  const want = new Set(waiting);
  const next: Numbers = {};
  for (const [id, x] of Object.entries(prev)) {
    if (want.has(id)) next[id] = { n: x.n };
    else if (x.freed === undefined) next[id] = { n: x.n, freed: now };
    else if (now - x.freed < HOLD_S) next[id] = x;
  }
  const taken = new Set(Object.values(next).map((x) => x.n));
  let n = 1;
  for (const id of waiting) {
    if (next[id]) continue;
    while (taken.has(n)) n++;
    next[id] = { n };
    taken.add(n);
  }
  return next;
}

export const NUMBERS_KEY = "retinue-operator-numbers";

export function keptNumbers(): Numbers {
  try {
    const kept = JSON.parse(localStorage.getItem(NUMBERS_KEY) ?? "{}");
    return kept && typeof kept === "object" ? kept : {};
  } catch {
    return {};
  }
}

export function keepNumbers(numbers: Numbers): void {
  try {
    localStorage.setItem(NUMBERS_KEY, JSON.stringify(numbers));
  } catch {
    // storage unavailable: the numbers hold while the page is open
  }
}

// --- nothing jumps while he reads ---------------------------------------------------------------------------------

const AWAY_MS = 60_000;
let hiddenAt = 0;
let releases = 0;
const subs = new Set<() => void>();
const bump = () => {
  releases++;
  for (const f of subs) f();
};
if (typeof document !== "undefined")
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") hiddenAt = Date.now();
    else if (hiddenAt && Date.now() - hiddenAt > AWAY_MS) bump();
  });

/** «New: N — show»: lay the list out again now. */
export const releaseHeld = () => bump();

/**
 * The order on screen, held while the page is in front of him: a new card does not push in (it is counted, for
 * «New: N»), one gone stays in its place, dimmed, until he asks for the new layout or comes back after a minute away.
 * `ids` is the order the list would have now.
 */
export function useHeld(ids: string[]): { shown: string[]; incoming: string[]; gone: Set<string> } {
  const release = useSyncExternalStore(
    (f) => {
      subs.add(f);
      return () => subs.delete(f);
    },
    () => releases,
    () => releases,
  );
  const shownRef = useRef<string[] | null>(null);
  const releaseRef = useRef(release);
  let shown = shownRef.current ?? ids;
  // nothing on screen yet, a release, or a page not in front: take the order as it is
  if (!shown.length || releaseRef.current !== release || document.visibilityState === "hidden") shown = ids;
  releaseRef.current = release;
  shownRef.current = shown;
  const have = new Set(shown);
  const want = new Set(ids);
  return { shown, incoming: ids.filter((id) => !have.has(id)), gone: new Set(shown.filter((id) => !want.has(id))) };
}
