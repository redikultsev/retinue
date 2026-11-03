// What the conversation keeps of a thing decided, made or still waiting, in one line each — the card itself is a tap
// away, never gone. A decided card is its receipt («You answered «Where to stay?» — Centre ›», «Allowed `git tag …` ›»),
// open on a tap to the card and its call's result; what still waits on the owner is a line pointing to «Waiting for you»
// above the input box, where its buttons are; sub-sessions' reports are one line that opens to them, their provenance
// kept.
import type * as React from "react";
import { ApprovalCard } from "@/components/approval-card";
import { Ban, Check, ChevronRight, Clock, Terminal } from "@/components/icons";
import { ProposalCard } from "@/components/proposal-card";
import { questionsOf, viewOf } from "@/components/question-card";
import { RelationIcon } from "@/components/relation";
import { oneLine } from "@/lib/format";
import { reportParts } from "@/lib/reports";
import type { Approval, Input, Item, Proposal } from "@/lib/types";
import { cn } from "@/lib/utils";
import { InputBubble } from "./input";

/** A line that opens to what it stands for: an icon, its words, a chevron. */
export function Receipt({ icon, children, label, testid = "receipt" }: { icon: React.ReactNode; label: React.ReactNode; children: React.ReactNode; testid?: string }) {
  return (
    <details data-testid={testid} className="group/step min-w-0">
      <summary className="-mx-1.5 flex min-h-7 cursor-pointer list-none items-center gap-2 rounded-md px-1.5 text-[13px] text-muted-foreground select-none transition-colors hover:bg-accent/60 hover:text-foreground pointer-coarse:min-h-11">
        <span className="shrink-0 [&_svg]:size-3.5">{icon}</span>
        <span className="min-w-0 flex-1 truncate text-foreground/80">{label}</span>
        <ChevronRight className="fold-chevron size-3 shrink-0 opacity-50 transition-transform group-hover/step:opacity-100" />
      </summary>
      <div className="mt-1 mb-2 flex min-w-0 flex-col gap-2">{children}</div>
    </details>
  );
}

const Code = ({ children }: { children: React.ReactNode }) => <code className="font-mono text-xs">{children}</code>;

/** «You answered «Where to stay?» — Centre»: the first question and its answer, and how many more. */
export function answeredWords(questions: string[], answers: string[]): React.ReactNode {
  const more = questions.length > 1 ? ` and ${questions.length - 1} more` : "";
  return (
    <>
      You answered «{questions[0] ?? "the question"}»{answers[0] ? ` — ${answers[0]}` : ""}
      {more}
    </>
  );
}

/** A card decided — an approval, a courier card, a question — as its receipt; its call's own line inside it. */
export function ApprovalReceipt({ approval: a, item }: { approval: Approval; item?: React.ReactNode }) {
  let icon: React.ReactNode;
  let label: React.ReactNode;
  const line = <Code>{oneLine(a.tool, a.input)}</Code>;
  if (a.kind === "question") {
    const view = viewOf(a);
    icon = <Check />;
    label = answeredWords(
      questionsOf(a.input).map((q) => q.question),
      Object.values(view.answers).map((x) => x.value),
    );
    if (!Object.keys(view.answers).length) label = <>Question · {a.state === "answered" ? "answered" : a.state}</>;
  } else if (a.kind === "courier") {
    icon = <Terminal />;
    label = <>Yours to run: {line}</>;
  } else if (a.state === "rejected") {
    icon = <Ban />;
    label = <>Denied {line}</>;
  } else if (a.state === "granted" || a.state === "executing" || a.state === "executed") {
    icon = <Check />;
    label = <>Allowed {line}</>;
  } else {
    icon = <Clock />;
    label = <>Not decided in time: {line}</>;
  }
  return (
    <Receipt icon={icon} label={label}>
      <ApprovalCard approval={a} />
      {item}
    </Receipt>
  );
}

const PROPOSAL_WORDS: Record<string, string> = { started: "Started", declined: "Set aside", expired: "Not started in time" };

/** A sub-session proposed and decided: started (a link to it inside), set aside, or past its week. */
export function ProposalReceipt({ proposal: p }: { proposal: Proposal }) {
  return (
    <Receipt
      icon={<RelationIcon how="sub-session" />}
      label={
        <>
          {PROPOSAL_WORDS[p.state] ?? p.state} the sub-session «{p.title}»
        </>
      }
    >
      <ProposalCard proposal={p} />
    </Receipt>
  );
}

/** A question answered with no card of its own any more: the transcript's item, as a receipt. */
export function QuestionItemReceipt({ item }: { item: Extract<Item, { kind: "question" }> }) {
  const questions = item.data.questions.map((q) => q.question);
  return (
    <Receipt icon={<Check />} label={answeredWords(questions, [item.data.answer ?? ""])}>
      <ul className="flex flex-col gap-1 rounded-lg border bg-card px-3 py-2.5 text-[13px]">
        {item.data.questions.map((q, i) => (
          <li key={i} className="flex flex-col gap-1">
            {q.header && <span className="text-xs text-muted-foreground">{q.header}</span>}
            <span className="font-medium">{q.question}</span>
            {q.options && <span className="text-muted-foreground">{q.options.map((o) => o.label).join(" · ")}</span>}
          </li>
        ))}
        <li>
          <span className="text-muted-foreground">Answer: </span>
          {item.data.answer}
        </li>
      </ul>
    </Receipt>
  );
}

/** Sub-sessions' reports to this session: one line with their titles, the reports themselves under it. */
export function ReportsReceipt({ input }: { input: Input }) {
  const { reports } = reportParts(input.text);
  const titles = reports.map((r) => r.title).join(", ");
  const label = reports.length
    ? `${reports.length} ${reports.length === 1 ? "sub-session" : "sub-sessions"} reported: ${titles}`
    : "A sub-session reported";
  return (
    <Receipt icon={<RelationIcon how="sub-session" />} label={label} testid="reports-receipt">
      <InputBubble input={input} />
    </Receipt>
  );
}

/** Where something that waits on the owner happened: one line, its buttons in «Waiting for you» below. */
export function WaitingAnchor({ label, onShow }: { label: React.ReactNode; onShow?: () => void }) {
  return (
    <button
      type="button"
      data-testid="waiting-anchor"
      onClick={onShow}
      className={cn(
        "-mx-1.5 flex min-h-7 w-full items-center gap-2 rounded-md px-1.5 text-left text-[13px] transition-colors hover:bg-accent/60 pointer-coarse:min-h-11",
      )}
    >
      <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-warning" />
      <span className="min-w-0 flex-1 truncate">
        <span className="text-muted-foreground">Waiting for you below: </span>
        {label}
      </span>
      <ChevronRight className="size-3 shrink-0 rotate-90 opacity-50" />
    </button>
  );
}
