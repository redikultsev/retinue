// «Results»: everything a session made, one list newest first (components/results.tsx) — the files it handed over from
// `out/` (a page among them by its type), its commits to the Base, its sub-sessions' reports, what its turn ending
// asked the owner to look at, a code session's diff and what waits to be pushed or was pushed. Read from what the app
// already has: the session's snapshot and stream, its row on the feed, the diff's stat. Pure, exported for tests.
import { reportParts } from "./reports";
import type { EndingArtifact, TurnEnding } from "./feed";
import { thingName, thingsOf } from "./operator";
import type { BaseCard, Input, OutFile, PushCard, Session, Summary } from "./types";

/** A code session's `git diff HEAD` in numbers: from the stat's last line. */
export interface DiffStat {
  files: number;
  added: number;
  removed: number;
}

export type Result =
  | { kind: "file" | "page"; key: string; at: number; file: OutFile }
  | { kind: "base"; key: string; at: number; id: string; card?: BaseCard; line?: string | null }
  | { kind: "report"; key: string; at: number; title: string; child: string; cut: boolean; text: string }
  | { kind: "look"; key: string; at: number; thing: EndingArtifact }
  | { kind: "diff"; key: string; at: number; stat: DiffStat }
  | { kind: "push"; key: string; at: number; count: number; why?: string }
  | { kind: "pr"; key: string; at: number; card: PushCard };

/** «3 files changed, 27 insertions(+), 3 deletions(-)» → its numbers; null when there is no change. */
export function parseStat(stat: string): DiffStat | null {
  const last = stat.trim().split("\n").at(-1) ?? "";
  const files = /(\d+) files? changed/.exec(last);
  if (!files) return null;
  return {
    files: Number(files[1]),
    added: Number(/(\d+) insertions?\(\+\)/.exec(last)?.[1] ?? 0),
    removed: Number(/(\d+) deletions?\(-\)/.exec(last)?.[1] ?? 0),
  };
}

/** A file of `out/` that is a page: an HTML file, shown in its sandboxed frame. */
export const isPage = (f: Pick<OutFile, "mime" | "path">) => f.mime.split(";")[0]!.trim() === "text/html";

export interface ResultsInput {
  session: Session;
  files: OutFile[];
  /** Every Base card the app has; the session's are picked. */
  bases: Record<string, BaseCard>;
  summaries: Summary[];
  inputs: Input[];
  ending?: TurnEnding | null;
  /** When the ending was said (seconds): its things are as new as it. */
  endingAt?: number;
  pushes: Record<string, PushCard>;
  diff?: DiffStat | null;
}

export function resultsOf({ session, files, bases, summaries, inputs, ending, endingAt, pushes, diff }: ResultsInput): Result[] {
  const out: Result[] = [];
  for (const f of files) out.push({ kind: isPage(f) ? "page" : "file", key: `f:${f.id}`, at: f.at, file: f });
  // a commit to the Base: its card when the app has it, else the summary's line — a tap fetches the card
  const seen = new Set<string>();
  for (const card of Object.values(bases))
    if (card.session === session.id && card.state !== "failed") {
      seen.add(card.id);
      out.push({ kind: "base", key: `b:${card.id}`, at: card.created, id: card.id, card });
    }
  for (const s of summaries)
    if (s.auto && !seen.has(s.auto)) {
      seen.add(s.auto);
      out.push({ kind: "base", key: `b:${s.auto}`, at: s.at, id: s.auto, line: s.base_line });
    }
  for (const input of inputs) {
    if (input.by !== "report") continue;
    reportParts(input.text).reports.forEach((r, k) => {
      out.push({ kind: "report", key: `r:${input.input_id}/${k}`, at: input.at, title: r.title, child: r.id, cut: r.cut, text: r.text });
    });
  }
  if (ending)
    thingsOf(ending).forEach((thing, k) => {
      out.push({ kind: "look", key: `l:${k}:${thingName(thing)}`, at: endingAt ?? session.at, thing });
    });
  for (const card of Object.values(pushes))
    if (card.session === session.id && card.state === "pushed" && card.url) out.push({ kind: "pr", key: `pr:${card.id}`, at: card.at, card });
  out.sort((a, b) => b.at - a.at);
  // what stands now — the diff of the work tree and what waits to be pushed — goes first
  const now: Result[] = [];
  if ((session.push ?? 0) > 0 || session.push_why)
    now.push({ kind: "push", key: "push", at: session.at, count: session.push ?? 0, why: session.push_why });
  if (diff && diff.files > 0) now.push({ kind: "diff", key: "diff", at: session.at, stat: diff });
  return [...now, ...out];
}

/** The tab a session opens on: a finished one shows what it made; one working or waiting, the conversation. */
export function defaultTab(session: Session | undefined, results: number): "chat" | "results" {
  return session && (session.state === "idle" || session.state === "closed") && results > 0 ? "results" : "chat";
}
