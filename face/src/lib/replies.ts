// A ping's suggested replies (retinue/pings.py). The model ends a turn that leaves the owner a choice with one line of
// suggested replies (`pings._LINE`); the platform parses it, keeps it out of the summary and sends it back as `Summary.line`, exactly
// as written. The turn's own last message in the transcript still ends with it: the face takes off that exact line —
// it never parses one itself, so it can never disagree with the platform about what was a replies line.
import type { Item, Summary } from "./types";

/** The text without `line` at its very end; anything else as it was. */
export function withoutLine(text: string, line: string | null | undefined): string {
  if (!line) return text;
  const end = text.trimEnd();
  return end.endsWith(line) ? end.slice(0, end.length - line.length).trimEnd() : text;
}

/** The items with each ping's replies line taken off its turn's last model message. Pure: the same items when no
 * ping has a line. */
export function hideReplyLines(items: Item[], summaries: Summary[]): Item[] {
  const lines = new Map<number, string>();
  for (const s of summaries) if (typeof s.turn === "number" && s.line) lines.set(s.turn, s.line);
  if (lines.size === 0) return items;
  const last = new Map<number, number>();
  items.forEach((it, k) => {
    if (it.kind === "message" && it.data.role === "assistant") last.set(it.turn, k);
  });
  return items.map((it, k) => {
    if (it.kind !== "message" || last.get(it.turn) !== k) return it;
    const text = withoutLine(it.data.text, lines.get(it.turn));
    return text === it.data.text ? it : { ...it, data: { ...it.data, text } };
  });
}

/** The session's latest summary when its replies are still pressable; a newer summary closes the older ones. */
export function openPing(summaries: Summary[]): Summary | null {
  const last = summaries.at(-1);
  return last && typeof last.seq === "number" && (last.replies?.length ?? 0) > 0 ? last : null;
}
