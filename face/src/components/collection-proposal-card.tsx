// A write to a collection an agent proposed (retinue/colltools.py): what each operation would change, the writer's
// words and why it is the owner's to apply — a card in the feed, a row on a Space's screen; «Apply» writes it as his own, «Dismiss» sets it aside. A record that moved
// on since says who moved it, and the card stays for him to dismiss.
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { Database } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { ApiError } from "@/lib/api";
import { valueWords } from "@/lib/collections";
import { actorWords, changeWords, conflictWords, editApi, type ProposalsApi, proposalsApi, refusalOf, type Step, undoSteps } from "@/lib/edit";
import { toast } from "@/lib/toast";
import type { CollectionProposal, CollectionProposalState } from "@/lib/view-types";
import { cn, inputId } from "@/lib/utils";
import { UNDO_MS, useEditing } from "./views/edit";

const STATE_WORDS: Record<CollectionProposalState, string> = {
  open: "proposed",
  applying: "applying",
  applied: "applied",
  dismissed: "dismissed",
  expired: "expired",
};
const SHOWN = 5;

/** The card's head: «Deals · 2 changes». */
export function proposalTitle(p: Pick<CollectionProposal, "collection" | "changes">): string {
  const n = p.changes.length;
  return `${valueWords(p.collection)} · ${n} ${n === 1 ? "change" : "changes"}`;
}

/** One operation in words: its record, and what it would do to it. */
export function proposedWords(p: CollectionProposal, i: number): { title: string; what: string } {
  const change = p.changes[i]!;
  return { title: change.title ?? "Untitled", what: changeWords(change, p.fields, p.words).join(" · ") };
}

interface Props {
  proposal: CollectionProposal;
  api?: ProposalsApi;
  /** Called once a press changed it. */
  onDecided?: (proposal: CollectionProposal) => void;
  /** A row of a Space's list rather than a card: its changes, its why, its two buttons. */
  row?: boolean;
  className?: string;
}

export function CollectionProposalCard({ proposal: given, api = proposalsApi, onDecided, row = false, className }: Props) {
  const [proposal, setProposal] = useState(given);
  const [busy, setBusy] = useState<"apply" | "dismiss" | null>(null);
  const [moved, setMoved] = useState<string | null>(null);
  const [all, setAll] = useState(false);
  const editing = useEditing();
  const current = given.at > proposal.at ? given : proposal;
  const waiting = current.state === "open";
  const shown = all ? current.changes : current.changes.slice(0, SHOWN);

  async function decide(action: "apply" | "dismiss") {
    setBusy(action);
    try {
      const res = await api.decide(current.id, action);
      setProposal(res.proposal);
      setMoved(null);
      const undo = res.answer ? undoSteps(res.answer) : [];
      if (undo.length) toast("Applied.", "info", UNDO_MS, undefined, { label: "Undo", run: () => void takeBack(current.collection, undo) });
      else toast(action === "apply" ? "Applied." : "Dismissed.");
      onDecided?.(res.proposal);
    } catch (e) {
      const refusal = e instanceof ApiError ? refusalOf(e.data) : null;
      if (refusal && (refusal.kind === "stale" || refusal.kind === "after")) setMoved(conflictWords(refusal));
      else toast(e instanceof ApiError ? e.message : "That didn't work.", "error");
    } finally {
      setBusy(null);
    }
  }

  /** «Undo» under «Applied.»: on a Space's screen, its editor's write — with its «Changed meanwhile» —; in the feed,
   * the write alone. */
  async function takeBack(collection: string, undo: Step[]) {
    if (editing) {
      await editing.write(collection, undo, "Undone.");
      return;
    }
    try {
      await editApi.write(collection, undo.map((s) => s.op), inputId());
      toast("Undone.");
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Not undone.", "error", 6000);
    }
  }

  const by = current.session ? (
    <Link to="/s/$id" params={{ id: current.session }} className="underline-offset-4 hover:underline">
      a session
    </Link>
  ) : (
    actorWords(current.actor)
  );
  if (row)
    return (
      <li data-testid="collection-proposal" className={cn("flex flex-col gap-2 px-3 py-2.5 sm:flex-row sm:items-start sm:gap-4", className)}>
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          {shown.map((_, i) => {
            const { title, what } = proposedWords(current, i);
            return (
              <span key={i} className="text-[13px] break-words">
                <span className="font-medium">{title}</span>
                <span className="text-muted-foreground">{` · ${what}`}</span>
              </span>
            );
          })}
          {current.changes.length > SHOWN && !all && (
            <button type="button" className="w-fit text-xs text-muted-foreground hover:text-foreground" onClick={() => setAll(true)}>
              {`Show all ${current.changes.length}`}
            </button>
          )}
          <span data-testid="proposal-why" className="text-xs break-words text-muted-foreground">
            {current.why ? `${current.why} — ` : ""}
            {valueWords(current.collection)}, proposed by {by}
            {current.needs && waiting ? `. Yours to apply: ${current.needs}` : ""}
          </span>
          {moved && (
            <span role="alert" className="text-xs text-amber-700 dark:text-warning">
              {`${moved} Dismiss it, or ask the session again.`}
            </span>
          )}
        </span>
        {waiting && (
          <span className="flex shrink-0 gap-1.5">
            <Button size="sm" disabled={busy !== null || moved !== null} onClick={() => void decide("apply")}>
              {busy === "apply" ? "Applying…" : "Apply"}
            </Button>
            <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => void decide("dismiss")}>
              {busy === "dismiss" ? "Sending…" : "Dismiss"}
            </Button>
          </span>
        )}
      </li>
    );
  return (
    <Card data-testid="collection-proposal" className={cn(waiting ? "border-primary/50 bg-primary/5" : "bg-muted/30", className)}>
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="flex min-w-0 items-center gap-2 text-sm">
            <Database className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            <span className="min-w-0 break-words">{proposalTitle(current)}</span>
          </CardTitle>
          <Badge variant={waiting ? "default" : "secondary"}>{STATE_WORDS[current.state]}</Badge>
        </div>
        <p className="text-[13px] text-muted-foreground">
          {current.session ? (
            <Link to="/s/$id" params={{ id: current.session }} className="underline-offset-4 hover:underline">
              A session
            </Link>
          ) : (
            valueWords(actorWords(current.actor))
          )}{" "}
          proposes this. Nothing is written until you apply it.
        </p>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {current.why && (
          <p data-testid="proposal-why" className="text-sm break-words whitespace-pre-wrap">
            {current.why}
          </p>
        )}
        <ul className="flex flex-col gap-1.5">
          {shown.map((_, i) => {
            const { title, what } = proposedWords(current, i);
            return (
              <li key={i} className="text-sm">
                <span className="font-medium break-words">{title}</span>
                <span className="block break-words text-muted-foreground">{what}</span>
              </li>
            );
          })}
        </ul>
        {current.changes.length > SHOWN && !all && (
          <Button variant="outline" size="sm" className="w-fit" onClick={() => setAll(true)}>
            {`Show all ${current.changes.length}`}
          </Button>
        )}
        {current.needs && waiting && <p className="text-xs break-words text-muted-foreground">{`Yours to apply: ${current.needs}`}</p>}
        {moved && (
          <p role="alert" className="text-sm text-amber-700 dark:text-warning">
            {`${moved} Dismiss it, or ask the session again.`}
          </p>
        )}
        {current.state === "expired" && <p className="text-sm text-muted-foreground">Not applied within a week.</p>}
      </CardContent>
      {waiting && (
        <CardFooter className="grid grid-cols-2 sm:flex">
          <Button disabled={busy !== null || moved !== null} onClick={() => void decide("apply")}>
            {busy === "apply" ? "Applying…" : "Apply"}
          </Button>
          <Button variant="outline" disabled={busy !== null} onClick={() => void decide("dismiss")}>
            {busy === "dismiss" ? "Sending…" : "Dismiss"}
          </Button>
        </CardFooter>
      )}
    </Card>
  );
}
