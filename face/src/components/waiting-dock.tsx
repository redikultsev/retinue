// «Waiting for you · N»: what in this session waits on the owner, above the input box — a card to allow, a question, a
// sub-session it proposed, what its turn ending asks (an answer, an approval, his step). One row each, its own buttons
// at its right when the whole of what it decides fits the row (a one-line command, a short brief); otherwise a button
// that opens the whole card under its row first — nothing is allowed or started unseen. The conversation keeps a
// one-line anchor where each came (transcript/receipts.tsx `WaitingAnchor`); a tap on it opens its row here.
import { useEffect, useMemo, useRef } from "react";
import type * as React from "react";
import { ApprovalCard, useDecision } from "@/components/approval-card";
import { EndingCard, mainOf } from "@/components/ending-card";
import { Check, ChevronRight, Flag, Question, Terminal } from "@/components/icons";
import { ProposalCard, shortBrief, useProposalPress } from "@/components/proposal-card";
import { questionsOf } from "@/components/question-card";
import { RelationIcon } from "@/components/relation";
import { waitingKey } from "@/components/transcript/transcript";
import { Button } from "@/components/ui/button";
import { type FeedRow, type TurnEnding, useFeed } from "@/lib/feed";
import { extraFields, inputText, markInput, oneLine } from "@/lib/format";
import { useLiveState } from "@/lib/live";
import { endingOf } from "@/lib/operator";
import { approvalsOf, proposalsOf, waitsOnOwner } from "@/lib/store";
import type { Approval, Proposal } from "@/lib/types";
import { cn } from "@/lib/utils";

export type Waiting =
  | { kind: "approval"; key: string; at: number; approval: Approval }
  | { kind: "proposal"; key: string; at: number; proposal: Proposal }
  | { kind: "ending"; key: string; at: number; row: FeedRow; ending: TurnEnding };

/** What waits on the owner in a session, oldest first. */
export function useWaiting(id: string): Waiting[] {
  const state = useLiveState();
  const feed = useFeed();
  const { approvals, proposals } = state;
  return useMemo(() => {
    const out: Waiting[] = [
      ...approvalsOf(approvals, id)
        .filter(waitsOnOwner)
        .map((a): Waiting => ({ kind: "approval", key: waitingKey.approval(a), at: a.created, approval: a })),
      ...proposalsOf(proposals, id)
        .filter((p) => p.state === "open")
        .map((p): Waiting => ({ kind: "proposal", key: waitingKey.proposal(p), at: p.created, proposal: p })),
    ];
    const row = feed?.groups.flatMap((g) => g.rows).find((r) => r.key === `ending:${id}`);
    const ending = row ? endingOf(row) : null;
    if (row && ending && (ending.kind === "ask" || ending.kind === "approve" || ending.kind === "step"))
      out.push({ kind: "ending", key: `e:${id}`, at: row.since, row, ending });
    return out.sort((a, b) => a.at - b.at);
  }, [approvals, proposals, feed, id]);
}

/** A command the row can show whole: one line, nothing hidden in it, no other field of the call to read. */
export function wholeOnRow(a: Approval): boolean {
  const body = markInput(inputText(a));
  return a.kind === "approval" && !body.marked && body.lines <= 1 && body.chars <= 160 && extraFields(a).length === 0;
}

const SMALL = "pointer-coarse:h-11";

function Shell({
  k,
  icon,
  title,
  line,
  actions,
  open,
  onOpen,
  more,
  children,
}: {
  k: string;
  icon: React.ReactNode;
  title: React.ReactNode;
  line?: React.ReactNode;
  actions?: React.ReactNode;
  open: boolean;
  onOpen: (open: boolean) => void;
  /** The button that opens the whole card when the row cannot decide it. */
  more?: string;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLLIElement>(null);
  useEffect(() => {
    if (open) ref.current?.scrollIntoView?.({ block: "nearest" });
  }, [open]);
  return (
    <li ref={ref} data-testid="waiting-row" data-key={k} className="flex scroll-mt-8 flex-col">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 px-2.5 py-1.5">
        <span className="flex min-w-0 flex-[1_1_16rem] items-start gap-2">
          <span className="mt-0.5 shrink-0 text-muted-foreground [&_svg]:size-3.5">{icon}</span>
          <span className="flex min-w-0 flex-col">
            <span className="text-[13px] font-medium break-words">{title}</span>
            {line && <span className="text-[13px] break-words text-muted-foreground">{line}</span>}
          </span>
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-1.5">
          {actions}
          {more && !open && (
            <Button variant="outline" size="sm" className={SMALL} onClick={() => onOpen(true)}>
              {more}
            </Button>
          )}
          <button
            type="button"
            aria-expanded={open}
            aria-label={open ? "Hide the card" : "Show the card"}
            title={open ? "Hide the card" : "Show the card"}
            onClick={() => onOpen(!open)}
            className="hit inline-flex size-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground pointer-coarse:size-11"
          >
            <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} />
          </button>
        </span>
      </div>
      {open && <div className="px-2.5 pb-2.5">{children}</div>}
    </li>
  );
}

function ApprovalRow({ a, open, onOpen }: { a: Approval; open: boolean; onOpen: (open: boolean) => void }) {
  const { busy, press } = useDecision(a);
  if (a.kind === "question") {
    const questions = questionsOf(a.input);
    return (
      <Shell
        k={waitingKey.approval(a)}
        icon={<Question />}
        title={questions[0]?.question ?? "A question"}
        line={questions.length > 1 ? `and ${questions.length - 1} more` : questions[0]?.options.map((o) => o.label).join(" · ")}
        more="Answer"
        open={open}
        onOpen={onOpen}
      >
        <ApprovalCard approval={a} />
      </Shell>
    );
  }
  const whole = wholeOnRow(a);
  const what = typeof a.input.description === "string" && a.input.description ? a.input.description : `Allow ${a.tool}`;
  return (
    <Shell
      k={waitingKey.approval(a)}
      icon={<Terminal />}
      title={what}
      line={<code className={cn("font-mono text-xs text-foreground/80", whole ? "break-all" : "block truncate")}>{whole ? inputText(a) : oneLine(a.tool, a.input)}</code>}
      more={whole ? undefined : "Review"}
      actions={
        whole && (
          <>
            <Button size="sm" className={SMALL} disabled={busy !== null} onClick={() => void press("approve")}>
              {busy === "approve" ? "Sending…" : "Allow"}
            </Button>
            <Button variant="outline" size="sm" className={SMALL} disabled={busy !== null} onClick={() => void press("reject")}>
              {busy === "reject" ? "Sending…" : "Deny"}
            </Button>
          </>
        )
      }
      open={open}
      onOpen={onOpen}
    >
      <ApprovalCard approval={a} />
    </Shell>
  );
}

function ProposalRow({ p, open, onOpen }: { p: Proposal; open: boolean; onOpen: (open: boolean) => void }) {
  const { busy, decide } = useProposalPress(p);
  // a short brief from the owner's own turn is read on the row whole; a long one, or one a report's turn wrote, opens first
  const whole = shortBrief(p.brief) && !p.from_report;
  return (
    <Shell
      k={waitingKey.proposal(p)}
      icon={<RelationIcon how="sub-session" />}
      title={`Sub-session: ${p.title}`}
      line={whole ? <span className="whitespace-pre-wrap">{p.brief}</span> : p.from_report ? "Proposed in a turn a report started, not you" : undefined}
      more={whole ? undefined : "Show the brief"}
      actions={
        whole && (
          <>
            <Button size="sm" className={SMALL} disabled={busy !== null} onClick={() => void decide("start")}>
              {busy === "start" ? "Starting…" : "Start"}
            </Button>
            <Button variant="outline" size="sm" className={SMALL} disabled={busy !== null} onClick={() => void decide("dismiss")}>
              {busy === "dismiss" ? "Sending…" : "Not now"}
            </Button>
          </>
        )
      }
      open={open}
      onOpen={onOpen}
    >
      <ProposalCard proposal={p} opened />
    </Shell>
  );
}

const ENDING: Record<string, { icon: React.ReactNode; more: string; word: string }> = {
  ask: { icon: <Question />, more: "Answer", word: "Asks" },
  approve: { icon: <Check />, more: "Review", word: "Asks to allow" },
  step: { icon: <Flag />, more: "Open", word: "Your step" },
};

function EndingRow({ w, open, onOpen }: { w: Extract<Waiting, { kind: "ending" }>; open: boolean; onOpen: (open: boolean) => void }) {
  const look = ENDING[w.ending.kind]!;
  const main = mainOf(w.ending) || w.row.essence;
  return (
    <Shell k={w.key} icon={look.icon} title={main || look.word} line={w.ending.request ? `You asked: ${w.ending.request}` : undefined} more={look.more} open={open} onOpen={onOpen}>
      <EndingCard row={w.row} e={w.ending} />
    </Shell>
  );
}

export function WaitingDock({ waiting, open, onOpen }: { waiting: Waiting[]; open: string | null; onOpen: (key: string | null) => void }) {
  if (!waiting.length) return null;
  const toggle = (key: string) => (on: boolean) => onOpen(on ? key : null);
  return (
    <section
      aria-label="Waiting for you"
      data-testid="waiting-dock"
      className="mx-auto mb-1.5 flex max-h-[45dvh] w-full max-w-[46rem] flex-col overflow-y-auto overscroll-contain rounded-lg border bg-card"
    >
      <h2 className="sticky top-0 z-[1] flex h-8 shrink-0 items-center gap-2 border-b bg-card px-2.5 text-[13px] font-medium">
        <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-warning" />
        Waiting for you
        <span className="font-mono text-xs font-normal text-muted-foreground tabular-nums">{waiting.length}</span>
      </h2>
      <ul className="flex flex-col divide-y">
        {waiting.map((w) =>
          w.kind === "approval" ? (
            <ApprovalRow key={w.key} a={w.approval} open={open === w.key} onOpen={toggle(w.key)} />
          ) : w.kind === "proposal" ? (
            <ProposalRow key={w.key} p={w.proposal} open={open === w.key} onOpen={toggle(w.key)} />
          ) : (
            <EndingRow key={w.key} w={w} open={open === w.key} onOpen={toggle(w.key)} />
          ),
        )}
      </ul>
    </section>
  );
}
