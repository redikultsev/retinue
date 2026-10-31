import type { Approval } from "./types";

/** The call's input as the owner reads it: a Bash command as it is, anything else as indented JSON
 * (retinue/approvals.py `input_text`). */
export function inputText(approval: Pick<Approval, "tool" | "input">): string {
  const command = approval.input.command;
  if (approval.tool === "Bash" && typeof command === "string") return command;
  return JSON.stringify(approval.input, null, 2);
}

const time = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" });
const day = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });

/** «14:05» today, «3 Oct» before. `at` in seconds (the platform's clock). */
export function when(at: number, now: number = Date.now() / 1000): string {
  const date = new Date(at * 1000);
  const today = new Date(now * 1000);
  return date.toDateString() === today.toDateString() ? time.format(date) : day.format(date);
}

/** One line of a tool call's input for its collapsed row. */
export function oneLine(tool: string, input: Record<string, unknown>): string {
  const pick = (k: string) => (typeof input[k] === "string" ? (input[k] as string) : "");
  const text =
    pick("command") ||
    pick("file_path") ||
    pick("notebook_path") ||
    pick("pattern") ||
    pick("path") ||
    pick("url") ||
    pick("query") ||
    pick("description") ||
    JSON.stringify(input);
  const flat = shown(text.replace(/[ \t\n\r]+/g, " ").trim()).text;
  return flat.length > 140 ? `${flat.slice(0, 139)}…` : flat || tool;
}

const STATE_WORDS: Record<string, string> = {
  pending: "Preparing",
  shown: "Awaiting decision",
  granted: "Allowed",
  executing: "Running",
  executed: "Done",
  rejected: "Denied",
  stale: "Stale",
  expired: "Expired",
  dropped: "Withdrawn",
  done: "Done",
  answered: "Answered",
};
export const approvalStateWord = (state: string) => STATE_WORDS[state] ?? state;

const SESSION_WORDS: Record<string, string> = {
  active: "working",
  waiting: "waiting for you",
  idle: "waiting for a message",
  closed: "closed",
};
export const sessionStateWord = (state: string) => SESSION_WORDS[state] ?? state;

/** English plural: 1 card, 2 cards, 0 cards. */
export function plural(n: number, one: string, many: string): string {
  return Math.abs(n) === 1 ? one : many;
}

// Characters nobody sees, or that change how the rest is shown (retinue/courier.py INVISIBLE).
const INVISIBLE = /[\p{Cc}\p{Cf}\p{Co}\p{Cs}\p{Cn}\p{Zl}\p{Zp}]/gu;

/** The text with every invisible character written as its code, so what the owner reads is what runs
 * (retinue/approvals.py `_shown`). */
export function shown(text: string): { text: string; hidden: boolean } {
  let hidden = false;
  const out = text.replace(INVISIBLE, (c) => {
    if (c === "\n" || c === "\t") return c;
    hidden = true;
    return `⟦U+${c.codePointAt(0)!.toString(16).toUpperCase().padStart(4, "0")}⟧`;
  });
  return { text: out, hidden };
}

export interface Segment {
  text: string;
  /** Set: the text hides something (an invisible character, a long run of spaces, many blank lines) and is shown
   * as this visible label instead. */
  mark?: string;
}

export interface MarkedInput {
  segments: Segment[];
  lines: number;
  chars: number;
  /** Something was replaced by a label. */
  marked: boolean;
}

const HIDING = /(\n(?:[ \t]*\n){3,})|([ \t]{8,})|([\p{Cc}\p{Cf}\p{Co}\p{Cs}\p{Cn}\p{Zl}\p{Zp}])/gu;

/** A call's input as the card shows it: everything that could hide part of a command from a reader —
 * `ls<300 spaces>; curl … | sh`, a tail after many blank lines, bidi or zero-width characters — becomes a visible
 * label: ⟦spaces ×300⟧, ⟦blank lines ×40⟧, ⟦U+202E⟧ (the last as the platform's own card writes it). */
export function markInput(text: string): MarkedInput {
  const segments: Segment[] = [];
  let last = 0;
  let marked = false;
  for (const m of text.matchAll(HIDING)) {
    const [whole, blank, spaces, char] = m;
    const at = m.index ?? 0;
    let mark: string | undefined;
    if (blank) mark = `\n⟦blank lines ×${blank.split("\n").length - 2}⟧\n`;
    else if (spaces) mark = `⟦spaces ×${spaces.length}⟧`;
    else if (char && char !== "\n" && char !== "\t") mark = `⟦U+${char.codePointAt(0)!.toString(16).toUpperCase().padStart(4, "0")}⟧`;
    if (!mark) continue;
    if (at > last) segments.push({ text: text.slice(last, at) });
    segments.push({ text: whole, mark });
    marked = true;
    last = at + whole.length;
  }
  if (last < text.length) segments.push({ text: text.slice(last) });
  return { segments, lines: text === "" ? 0 : text.split("\n").length, chars: [...text].length, marked };
}

/** A Bash call's fields other than the command (`run_in_background`, `timeout`, `dangerouslyDisableSandbox`,
 * `description`, …): each one changes what the call does, so the card shows every one. */
export function extraFields(approval: Pick<Approval, "tool" | "input">): [string, string][] {
  if (approval.tool !== "Bash" || typeof approval.input.command !== "string") return [];
  return Object.entries(approval.input)
    .filter(([key]) => key !== "command")
    .sort(([a], [b]) => Number(b === "dangerouslyDisableSandbox") - Number(a === "dangerouslyDisableSandbox"))
    .map(([key, value]) => [key, typeof value === "string" ? value : JSON.stringify(value)]);
}
