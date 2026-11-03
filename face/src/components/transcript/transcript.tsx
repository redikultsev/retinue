// A session's conversation: its items in (turn, n) order, each summary at the end of the turn it was said for, the
// owner's own messages where they came (with what he attached), and the live text of a running turn. Consecutive tool
// calls fold into one line («7 steps · …», as Claude's desktop app folds them): open while the turn runs them, folded
// once it moved on. A call with a card — an approval, a courier or question card, a proposed sub-session — is never
// folded and its raw line is not shown: where the card still waits on the owner a one-line anchor points to «Waiting
// for you» above the input box (components/waiting-dock.tsx), where its buttons are; once decided it is a one-line
// receipt that opens to the card and the call (receipts.tsx). What a turn wrote to the Base is one line under its
// summary; sub-sessions' reports are one line that opens to them.
import { type ReactNode, useMemo } from "react";
import { BaseLine } from "@/components/base-card";
import { questionsOf } from "@/components/question-card";
import { when } from "@/lib/format";
import { waitsOnOwner } from "@/lib/store";
import type { Approval, Input, Item, Proposal, Summary } from "@/lib/types";
import { cn } from "@/lib/utils";
import { InputBubble } from "./input";
import { isStep, Markdown, StepRun, TranscriptItem } from "./item";
import { ApprovalReceipt, ProposalReceipt, QuestionItemReceipt, ReportsReceipt, WaitingAnchor } from "./receipts";

/** The keys «Waiting for you» knows its rows by: an anchor in the conversation opens its row. */
export const waitingKey = { approval: (a: Approval) => `a:${a.id}`, proposal: (p: Proposal) => `p:${p.id}` };

/** A summary's head without the platform's ids and track («Result · notes · session e5f6a7b8» → «Result»), and when. */
export function headWord(summary: Summary): string {
  const word = summary.head.split(" · ")[0]?.trim() || "Result";
  return `${word} · ${when(summary.at)}`;
}

/** What a waiting card is, in the anchor's words. */
function waitingLabel(a: Approval): string {
  if (a.kind === "question") return `«${questionsOf(a.input)[0]?.question ?? "a question"}»`;
  const what = typeof a.input.description === "string" && a.input.description ? a.input.description : a.tool;
  return `Allow ${a.tool}: ${what}`;
}

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

function SummaryNote({ summary, repeats, base, focused }: { summary: Summary; repeats: boolean; base?: ReactNode; focused?: boolean }) {
  // What the turn wrote to the Base.
  const buttons = base ? <div className="flex flex-col">{base}</div> : null;
  if (repeats)
    return (
      <div data-testid="summary" data-turn={summary.turn ?? undefined} className={cn("flex scroll-mt-3 flex-col gap-2", focused && "rounded-lg p-2 ring-2 ring-primary/50")}>
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground/80">
          <span className="h-px flex-1 bg-border" />
          <span className="truncate">{headWord(summary)}</span>
          <span className="h-px flex-1 bg-border" />
        </div>
        {buttons}
      </div>
    );
  return (
    <div
      data-testid="summary"
      data-turn={summary.turn ?? undefined}
      className={cn("flex scroll-mt-3 flex-col gap-1.5 rounded-lg bg-muted/60 px-3 py-2.5", focused && "ring-2 ring-primary/50")}
    >
      <div className="text-[11px] text-muted-foreground">{headWord(summary)}</div>
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
  /** The sub-sessions it proposed (retinue/subs.py): each under its call. */
  proposals?: Proposal[];
  /** An anchor pressed: the key of its row in «Waiting for you» (`waitingKey`). */
  onWaiting?: (key: string) => void;
  /** A turn to mark: its summary gets a ring (`?turn=N`). */
  focus?: number;
}

export function Transcript({ items, approvals, delta, running, summaries = [], inputs = [], proposals = [], focus, onWaiting }: Props) {
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
  const note = (s: Summary, index: number) => (
    <SummaryNote
      key={s.seq ?? s.at}
      summary={s}
      focused={focus !== undefined && s.turn === focus}
      repeats={s.text.trim() === turnText(items, index)}
      base={s.base_line ? <BaseLine id={s.auto} line={s.base_line} /> : null}
    />
  );
  // A card that waits is an anchor here, its buttons below; one decided is its receipt.
  const card = (a: Approval, item?: ReactNode) =>
    waitsOnOwner(a) ? (
      <WaitingAnchor key={a.id} label={waitingLabel(a)} onShow={() => onWaiting?.(waitingKey.approval(a))} />
    ) : (
      <ApprovalReceipt key={a.id} approval={a} item={item} />
    );
  const offer = (p: Proposal) =>
    p.state === "open" ? (
      <WaitingAnchor key={p.id} label={`Sub-session «${p.title}»`} onShow={() => onWaiting?.(waitingKey.proposal(p))} />
    ) : (
      <ProposalReceipt key={p.id} proposal={p} />
    );
  const message = (x: Input) => (x.by === "report" ? <ReportsReceipt key={x.input_id} input={x} /> : <InputBubble key={x.input_id} input={x} />);
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
              {mine.get(first)?.map(message)}
              <StepRun items={run.at.map((k) => items[k]!)} open={running && r === runs.length - 1} />
              {notes.get(last)?.map((s) => note(s, last))}
            </div>
          );
        const k = first;
        const item = items[k]!;
        return (
          <div key={`${item.turn}/${item.n}`} className="flex flex-col gap-3 empty:hidden">
            {mine.get(k)?.map(message)}
            {carded.has(k) ? (
              // the call's raw line is the card's: it opens inside the receipt, never on its own
              <>
                {cards.get(k)?.map((a, i) => card(a, i === 0 && item.kind !== "question" ? <TranscriptItem item={item} /> : undefined))}
                {offers.get(k)?.map(offer)}
              </>
            ) : item.kind === "result" && item.data.stop_reason === "tool_deferred" && approvals.length > 0 ? null : item.kind === "question" && item.data.answer !== null ? (
              <QuestionItemReceipt item={item} />
            ) : (
              <TranscriptItem item={item} />
            )}
            {notes.get(k)?.map((s) => note(s, k))}
          </div>
        );
      })}
      {cards.get(-1)?.map((a) => card(a))}
      {offers.get(-1)?.map(offer)}
      {mine.has(-1) && notesBefore.map((s) => note(s, -1))}
      {mine.get(-1)?.map(message)}
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
