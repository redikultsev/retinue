// A sub-session a session proposed instead of starting it (retinue/subs.py), as the desktop app's own-initiative card:
// its title, its brief — the model's text, shown as plain text and whole before «Start» (a long one opens first:
// nothing is started unseen) — and «Start» / «Not now». The session goes on meanwhile. Decided, the card says what
// became of it: started (a link to the sub-session), set aside, or past its week.
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { Fork } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { ApiError, api } from "@/lib/api";
import { type Live, live as defaultLive, useLiveState } from "@/lib/live";
import { toast } from "@/lib/toast";
import type { Proposal, ProposalState } from "@/lib/types";
import { cn } from "@/lib/utils";

const SHORT_CHARS = 400;
const SHORT_LINES = 6;
const STATE_WORDS: Record<ProposalState, string> = {
  open: "proposed",
  started: "started",
  declined: "set aside",
  expired: "expired",
};

/** The brief is short enough to read at a glance: shown whole at once. */
export const shortBrief = (brief: string) => brief.length <= SHORT_CHARS && brief.split("\n").length <= SHORT_LINES;

interface Props {
  proposal: Proposal;
  live?: Live;
  press?: typeof api.proposal;
  /** Called once a press changed it. */
  onDecided?: (proposal: Proposal) => void;
  className?: string;
}

export function ProposalCard({ proposal: given, live = defaultLive, press = api.proposal, onDecided, className }: Props) {
  // The card as the app knows it now: a press elsewhere may be newer than the copy it was given.
  const known = useLiveState().proposals[given.id];
  const proposal = known && known.at >= given.at ? known : given;
  const [open, setOpen] = useState(() => shortBrief(proposal.brief));
  const [busy, setBusy] = useState<"start" | "dismiss" | null>(null);
  const waiting = proposal.state === "open";

  async function decide(action: "start" | "dismiss") {
    setBusy(action);
    try {
      const res = await press(proposal.id, action);
      if (res.say) toast(res.say);
      live.dispatch({ type: "event", id: null, event: { kind: "proposal", data: res.proposal } });
      onDecided?.(res.proposal);
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "That didn't work.", "error");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card
      data-testid="proposal-card"
      className={cn(waiting ? "border-primary/50 bg-primary/5" : "bg-muted/30", className)}
    >
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="flex min-w-0 items-center gap-2 text-sm">
            <Fork className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            <span className="min-w-0 break-words">Sub-session: {proposal.title}</span>
          </CardTitle>
          <Badge variant={waiting ? "default" : "secondary"}>{STATE_WORDS[proposal.state]}</Badge>
        </div>
        {waiting && (
          <p className="text-[13px] text-muted-foreground">
            The session proposes to hand this to a session of its own and goes on meanwhile.
          </p>
        )}
      </CardHeader>
      <CardContent>
        {open ? (
          <pre
            data-testid="proposal-brief"
            className="max-h-72 overflow-auto rounded-md bg-muted px-2.5 py-2 font-sans text-[13px] leading-snug break-words whitespace-pre-wrap"
          >
            {proposal.brief}
          </pre>
        ) : (
          <div className="flex flex-col items-start gap-2">
            <p className="line-clamp-2 text-[13px] break-words whitespace-pre-wrap text-muted-foreground">{proposal.brief}</p>
            <Button variant="outline" size="sm" aria-expanded={false} onClick={() => setOpen(true)}>
              Show the brief
            </Button>
          </div>
        )}
        {proposal.state === "started" && proposal.child && (
          <Link
            to="/s/$id"
            params={{ id: proposal.child }}
            className="mt-2 inline-block text-sm text-primary underline-offset-4 hover:underline"
          >
            Open the sub-session
          </Link>
        )}
        {proposal.state === "expired" && (
          <p className="mt-2 text-sm text-muted-foreground">Not started within a week: ask the session again if you still want it.</p>
        )}
      </CardContent>
      {waiting && open && (
        <CardFooter className="grid grid-cols-2 sm:flex">
          <Button disabled={busy !== null} onClick={() => void decide("start")}>
            {busy === "start" ? "Starting…" : "Start"}
          </Button>
          <Button variant="outline" disabled={busy !== null} onClick={() => void decide("dismiss")}>
            {busy === "dismiss" ? "Sending…" : "Not now"}
          </Button>
        </CardFooter>
      )}
    </Card>
  );
}
