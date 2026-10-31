// An approval card (or a courier card) as the face shows it: the header lines code built, the FULL input in a
// monospace block (never cut: the face has no 4096 limit; wrapped, so nothing hides past the right edge; long
// whitespace runs, many blank lines and invisible characters shown as labels), every other field of a Bash call,
// the status, and «Allow» / «Deny» while it can be pressed. The decision is bound to the card's digest,
// exactly as a Telegram press. A question card (AskUserQuestion) is drawn by QuestionCard wherever a card is shown.
import { useState } from "react";
import { QuestionCard } from "@/components/question-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { ApiError, api } from "@/lib/api";
import { approvalStateWord, extraFields, inputText, markInput, plural } from "@/lib/format";
import { type Live, live as defaultLive } from "@/lib/live";
import { toast } from "@/lib/toast";
import type { Approval } from "@/lib/types";
import { cn } from "@/lib/utils";

interface Props {
  approval: Approval;
  live?: Live;
  decide?: typeof api.decide;
  /** A question card's answer (QuestionCard). */
  answer?: typeof api.answer;
  className?: string;
  /** Called with the card as it now reads once a press decided it (not when it was stale). */
  onDecided?: (approval: Approval) => void;
}

export function ApprovalCard(props: Props) {
  if (props.approval.kind === "question") {
    const { approval, live, answer, className, onDecided } = props;
    return <QuestionCard approval={approval} {...{ live, answer, className, onDecided }} />;
  }
  return <DecisionCard {...props} />;
}

function DecisionCard({ approval, live = defaultLive, decide = api.decide, className, onDecided }: Props) {
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const courier = approval.kind === "courier";
  const pressable = !courier && approval.state === "shown";
  const [title, ...head] = approval.view.head.length ? approval.view.head : [`Approval · ${approval.tool}`];
  // The last header line names the input («Input, N characters:»); the face shows the input itself below it.
  const body = markInput(inputText(approval));
  const fields = extraFields(approval);
  const link = approval.view.link && approval.view.link.length === 2 ? approval.view.link : null;

  async function press(action: "approve" | "reject") {
    setBusy(action);
    try {
      const res = await decide(approval, action);
      toast(res.toast);
      // No event id: the store keeps a newer copy the stream may already have brought.
      if (res.approval) live.dispatch({ type: "event", id: null, event: { kind: "approval", data: res.approval } });
      if (res.approval && res.approval.state !== "shown" && res.approval.state !== "stale") onDecided?.(res.approval);
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "That didn't work.", "error");
    } finally {
      setBusy(null);
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(inputText(approval));
      toast("Command copied.");
    } catch {
      toast("Couldn't copy — select the text by hand.", "error");
    }
  }

  return (
    <Card
      data-testid="approval-card"
      className={cn(pressable && "border-warning/70 bg-warning/5 ring-1 ring-warning/30", courier && "border-dashed", className)}
    >
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="text-sm">{title}</CardTitle>
          <Badge variant={pressable ? "warning" : "secondary"}>{approvalStateWord(approval.state)}</Badge>
        </div>
        {head.length > 0 && (
          <div className="flex flex-col gap-0.5 text-[13px] text-muted-foreground">
            {head.map((line, i) => (
              <div key={i} className={cn("break-words", line.startsWith("Warning") && "font-medium text-destructive")}>
                {line}
              </div>
            ))}
          </div>
        )}
      </CardHeader>
      <CardContent>
        <div className="mb-1 text-[11px] text-muted-foreground">
          {body.lines} {plural(body.lines, "line", "lines")} · {body.chars}{" "}
          {plural(body.chars, "character", "characters")}
        </div>
        <pre
          data-testid="approval-input"
          className="max-h-72 overflow-auto rounded-md bg-muted px-2.5 py-2 font-mono text-xs leading-snug break-all whitespace-pre-wrap"
        >
          {body.segments.map((seg, i) =>
            seg.mark ? (
              <mark key={i} title="hidden in the input" className="rounded-sm bg-destructive/15 px-0.5 font-semibold text-destructive">
                {seg.mark}
              </mark>
            ) : (
              seg.text
            ),
          )}
        </pre>
        {body.marked && (
          <p className="mt-2 text-sm font-medium text-destructive">
            The input hides something: long runs of spaces, blank lines or invisible characters — shown as ⟦…⟧ labels
          </p>
        )}
        {fields.length > 0 && (
          <dl data-testid="approval-fields" className="mt-2 flex flex-col gap-0.5 font-mono text-xs">
            {fields.map(([key, value]) => (
              <div key={key} className={cn("break-all", key === "dangerouslyDisableSandbox" && "font-semibold text-destructive")}>
                <dt className="inline">{key}: </dt>
                <dd className="inline">{value}</dd>
              </div>
            ))}
          </dl>
        )}
        {approval.view.status && <p className="mt-2 text-sm">{approval.view.status}</p>}
        {link && (
          <a className="mt-2 inline-block text-sm text-primary underline" href={link[1]} rel="noopener noreferrer" target="_blank">
            {link[0]}
          </a>
        )}
      </CardContent>
      {pressable && (
        <CardFooter className="grid grid-cols-2 sm:flex">
          <Button disabled={busy !== null} onClick={() => press("approve")}>
            {busy === "approve" ? "Sending…" : "Allow"}
          </Button>
          <Button variant="outline" disabled={busy !== null} onClick={() => press("reject")}>
            {busy === "reject" ? "Sending…" : "Deny"}
          </Button>
        </CardFooter>
      )}
      {courier && (
        <CardFooter>
          <Button variant="outline" onClick={copy}>
            Copy command
          </Button>
        </CardFooter>
      )}
    </Card>
  );
}
