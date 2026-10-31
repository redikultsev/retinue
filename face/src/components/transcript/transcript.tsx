// A session's transcript: its items in (turn, n) order, each approval, courier or question card inline after the call
// it is about (a question's card in place of its item), each summary at the end of the turn it was said for, the
// owner's own messages where they came (with what he attached), and the live text of a running turn. Consecutive tool
// calls fold into one line («7 steps · …», as Claude's desktop app folds them): open while the turn runs them, folded
// once it moved on. A call with a card is never folded — the card is the owner's to answer.
import { type ReactNode, useMemo } from "react";
import { ApprovalCard } from "@/components/approval-card";
import { AutoButton, BaseButton } from "@/components/base-card";
import { ProposalCard } from "@/components/proposal-card";
import { PushButton } from "@/components/push-card";
import { hideSphereLines } from "@/lib/sphere-line";
import type { Approval, Input, Item, Proposal, Summary } from "@/lib/types";
import { InputBubble } from "./input";
import { isStep, Markdown, StepRun, TranscriptItem } from "./item";

function callIds(item: Item): string[] {
  switch (item.kind) {
    case "reads":
      return item.data.calls.map((c) => c.id);
    case "edit":
    case "plan":
    case "subagent":
    case "question":
    case "tool":
      return [item.data.id];
    default:
      return [];
  }
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** Where each card goes: after the index of the item it is about, or -1 (the end). Pure, exported for tests. */
export function placeCards(items: Item[], approvals: Approval[]): Map<number, Approval[]> {
  const at = new Map<number, Approval[]>();
  for (const a of approvals) {
    let index = -1;
    if (a.tool_use_id) index = items.findIndex((it) => callIds(it).includes(a.tool_use_id!));
    if (index < 0) {
      for (let k = items.length - 1; k >= 0; k--) {
        const it = items[k]!;
        if (
          (it.kind === "tool" && it.data.tool === a.tool && same(it.data.input, a.input)) ||
          (it.kind === "edit" && it.data.tool === a.tool && it.data.path === a.input.file_path)
        ) {
          index = k;
          break;
        }
      }
    }
    at.set(index, [...(at.get(index) ?? []), a]);
  }
  return at;
}

/** Where each proposed sub-session goes: after the index of the call that proposed it, or -1 (the end). Pure, exported
 * for tests. */
export function placeProposals(items: Item[], proposals: Proposal[]): Map<number, Proposal[]> {
  const at = new Map<number, Proposal[]>();
  for (const p of proposals) {
    const index = items.findIndex((it) => callIds(it).includes(p.tool_use_id));
    at.set(index, [...(at.get(index) ?? []), p]);
  }
  return at;
}

/** Where each summary goes: after the index of the last item of its turn (or of an earlier turn), or -1 (the end).
 * A summary without a turn (said before the platform recorded one) goes to the end only if it is the latest. Pure,
 * exported for tests. */
export function placeSummaries(items: Item[], summaries: Summary[]): Map<number, Summary[]> {
  const at = new Map<number, Summary[]>();
  const latest = summaries.at(-1);
  for (const s of summaries) {
    let index = -1;
    if (typeof s.turn === "number") {
      for (let k = items.length - 1; k >= 0; k--) {
        if (items[k]!.turn <= s.turn) {
          index = k;
          break;
        }
      }
      if (index === items.length - 1) index = -1; // after the last item: the end, below the live text
    } else if (s !== latest) continue;
    at.set(index, [...(at.get(index) ?? []), s]);
  }
  return at;
}

/** Where each owner's message goes: before the index of the first item at or after its (turn, n) — what the session
 * had not yet written when it came — or -1 (the end). A message from before its place was recorded is not shown. Pure,
 * exported for tests. */
export function placeInputs(items: Item[], inputs: Input[]): Map<number, Input[]> {
  const at = new Map<number, Input[]>();
  for (const input of inputs) {
    if (typeof input.turn !== "number" || typeof input.n !== "number") continue;
    const { turn, n } = input;
    const index = items.findIndex((it) => it.turn > turn || (it.turn === turn && it.n >= n));
    at.set(index, [...(at.get(index) ?? []), input]);
  }
  return at;
}

/** The items as runs: consecutive calls (`isStep`) form one run unless something stands between them — an owner's
 * message placed before the next (`mine`), a summary after the previous (`notes`) — and a call with a card stands
 * alone. Each run is `{ steps, at }`: whether it folds, and its item indices. Pure, exported for tests. */
export function foldSteps(
  items: Item[],
  cards: ReadonlyMap<number, unknown>,
  notes: ReadonlyMap<number, unknown>,
  mine: ReadonlyMap<number, unknown>,
): { steps: boolean; at: number[] }[] {
  const runs: { steps: boolean; at: number[] }[] = [];
  items.forEach((item, k) => {
    const step = isStep(item) && !cards.has(k);
    const last = runs.at(-1);
    if (step && last?.steps && !mine.has(k) && !notes.has(last.at.at(-1)!)) last.at.push(k);
    else runs.push({ steps: step, at: [k] });
  });
  return runs;
}

/** The text of the last model message at or before `index`, within the same turn: a summary that only repeats it is
 * shown as its header line alone. */
function turnText(items: Item[], index: number): string {
  const end = index < 0 ? items.length - 1 : index;
  const turn = items[end]?.turn;
  for (let k = end; k >= 0 && items[k]!.turn === turn; k--) {
    const it = items[k]!;
    if (it.kind === "message" && it.data.role === "assistant") return it.data.text.trim();
  }
  return "";
}

function SummaryNote({ summary, repeats, base }: { summary: Summary; repeats: boolean; base?: ReactNode }) {
  // «To the Base · N» on the last one while the session has Base files uncommitted.
  const buttons = base ? <div className="flex flex-wrap items-center gap-1.5">{base}</div> : null;
  if (repeats)
    return (
      <div data-testid="summary" className="flex flex-col gap-2">
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground/80">
          <span className="h-px flex-1 bg-border" />
          <span className="truncate">{summary.head}</span>
          <span className="h-px flex-1 bg-border" />
        </div>
        {buttons}
      </div>
    );
  return (
    <div data-testid="summary" className="flex flex-col gap-1.5 rounded-lg bg-muted/60 px-3 py-2.5">
      <div className="text-[11px] text-muted-foreground">{summary.head}</div>
      <Markdown text={summary.text} />
      {buttons}
    </div>
  );
}

interface Props {
  items: Item[];
  approvals: Approval[];
  delta?: string;
  running: boolean;
  /** The session's summaries, oldest first: each at the end of its turn. */
  summaries?: Summary[];
  /** The owner's messages, oldest first: each before what the session wrote after it. */
  inputs?: Input[];
  /** The session's Base files not committed yet (retinue/tobase.py): «To the Base · N» under its last summary. */
  base?: { session: string; count: number };
  /** Its commits no branch of the remote has (retinue/topush.py): «Push · N» there too. */
  push?: { session: string; count: number; why?: string };
  /** The sub-sessions it proposed (retinue/subs.py): each card under its call. */
  proposals?: Proposal[];
}

export function Transcript({ items: given, approvals, delta, running, summaries = [], inputs = [], base, push, proposals = [] }: Props) {
  // A ping's sphere line is the platform's, not words: off the message it ends.
  const items = useMemo(() => hideSphereLines(given, summaries), [given, summaries]);
  const cards = useMemo(() => placeCards(items, approvals), [items, approvals]);
  const notes = useMemo(() => placeSummaries(items, summaries), [items, summaries]);
  const mine = useMemo(() => placeInputs(items, inputs), [items, inputs]);
  const offers = useMemo(() => placeProposals(items, proposals), [items, proposals]);
  // A call with a card of either kind stands alone, never folded.
  const carded = useMemo(() => new Map<number, unknown>([...cards, ...offers]), [cards, offers]);
  const runs = useMemo(() => foldSteps(items, carded, notes, mine), [items, carded, notes, mine]);
  // At the end, a summary said before the owner's newest message stays above it; the rest stay below the live text.
  const lastNotes = notes.get(-1) ?? [];
  const firstEnd = mine.get(-1)?.[0]?.at ?? Number.POSITIVE_INFINITY;
  const notesBefore = lastNotes.filter((s) => s.at <= firstEnd);
  const notesAfter = lastNotes.filter((s) => s.at > firstEnd);
  // A summary is usually the turn's last message again: then only its header marks the turn's end.
  const latest = summaries.at(-1);
  const note = (s: Summary, index: number) => (
    <SummaryNote
      key={s.seq ?? s.at}
      summary={s}
      repeats={s.text.trim() === turnText(items, index)}
      base={
        s === latest || s.auto ? (
          <>
            {s.auto && <AutoButton id={s.auto} />}
            {s === latest && base && base.count > 0 && <BaseButton session={base.session} count={base.count} />}
            {s === latest && push && push.count > 0 && <PushButton session={push.session} count={push.count} />}
            {s === latest && push?.why && <p className="text-xs text-muted-foreground">Cannot push — {push.why}</p>}
          </>
        ) : null
      }
    />
  );
  return (
    <div className="flex flex-col gap-3">
      {items.length === 0 && inputs.length === 0 && !delta && (
        <p className="py-8 text-center text-sm text-muted-foreground">Nothing yet.</p>
      )}
      {runs.map((run, r) => {
        const first = run.at[0]!;
        const last = run.at.at(-1)!;
        if (run.steps && run.at.length > 1)
          return (
            <div key={`${items[first]!.turn}/${items[first]!.n}`} className="flex flex-col gap-3">
              {mine.get(first)?.map((x) => <InputBubble key={x.input_id} input={x} />)}
              <StepRun items={run.at.map((k) => items[k]!)} open={running && r === runs.length - 1} />
              {notes.get(last)?.map((s) => note(s, last))}
            </div>
          );
        const k = first;
        const item = items[k]!;
        return (
          <div key={`${item.turn}/${item.n}`} className="flex flex-col gap-3">
            {mine.get(k)?.map((x) => <InputBubble key={x.input_id} input={x} />)}
            {/* A question with its card: the card shows the questions, and answers them. */}
            {!(item.kind === "question" && cards.get(k)?.some((a) => a.kind === "question")) && <TranscriptItem item={item} />}
            {cards.get(k)?.map((a) => <ApprovalCard key={a.id} approval={a} />)}
            {offers.get(k)?.map((p) => <ProposalCard key={p.id} proposal={p} />)}
            {notes.get(k)?.map((s) => note(s, k))}
          </div>
        );
      })}
      {cards.get(-1)?.map((a) => <ApprovalCard key={a.id} approval={a} />)}
      {offers.get(-1)?.map((p) => <ProposalCard key={p.id} proposal={p} />)}
      {mine.has(-1) && notesBefore.map((s) => note(s, -1))}
      {mine.get(-1)?.map((x) => <InputBubble key={x.input_id} input={x} />)}
      {delta ? (
        <div data-testid="delta" className="min-w-0">
          <Markdown text={delta} streaming />
        </div>
      ) : (
        running && (
          <div className="flex items-center gap-2 text-[13px] text-muted-foreground" aria-live="polite">
            <span className="size-1.5 animate-pulse rounded-full bg-primary" />
            <span className="animate-pulse">Working…</span>
          </div>
        )
      )}
      {(mine.has(-1) ? notesAfter : lastNotes).map((s) => note(s, -1))}
    </div>
  );
}
