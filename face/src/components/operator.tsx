// «Today» as the operator sees it: only the sessions' requests, one card each, answered in place — a card drawn by how
// the session's turn ended (components/ending-card.tsx), else by the card it shows (a question, an approval, a
// proposal, a push). A card that waits has a short number of its own. The order holds while he reads: a new card is
// counted at the top, one answered or gone stays in its place, dimmed, until he lays the list out again. No key
// answers a card here: one press would reach them all.
import { Link } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { ApprovalCard } from "@/components/approval-card";
import { CollectionProposalCard } from "@/components/collection-proposal-card";
import { EndingCard, FRAME } from "@/components/ending-card";
import { ProposalCard } from "@/components/proposal-card";
import { PushButton } from "@/components/push-card";
import { QuestionDecide } from "@/components/question-decide";
import { Crumb, deliverTo, Reply } from "@/components/reply";
import { Button } from "@/components/ui/button";
import { type Feed, type FeedRow, feedSource, waited } from "@/lib/feed";
import { endingOf, keepNumbers, keptNumbers, type Numbers, type OperatorView, releaseHeld, renumber, useHeld, verbOf } from "@/lib/operator";
import { useProjectOf } from "@/lib/projects";
import { projectTitle } from "@/lib/today";
import type { Sphere } from "@/lib/types";
import { cn } from "@/lib/utils";
import { requests, waitingKeys, waits } from "@/lib/waiting";

/** The operator as cards or as the map of its sessions in their places: its own place and a project's tab alike. */
export function OperatorSwitch({ view, onView }: { view: OperatorView; onView: (v: OperatorView) => void }) {
  return (
    <fieldset aria-label="Show" className="inline-flex h-9 w-fit items-center self-start rounded-md border p-0.5 max-md:h-11">
      {(["cards", "map"] as const).map((v) => (
        <button
          key={v}
          type="button"
          aria-pressed={view === v}
          onClick={() => onView(v)}
          className={cn("h-7 rounded px-3 text-[13px] max-md:h-9 whitespace-nowrap transition-colors", view === v ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground")}
        >
          {v === "cards" ? "Cards" : "Map"}
        </button>
      ))}
    </fieldset>
  );
}

function Body({ row }: { row: FeedRow }) {
  const refresh = () => void feedSource.refresh();
  const e = endingOf(row);
  if (e) return <EndingCard row={row} e={e} />;
  if (row.approval?.kind === "question")
    return (
      <div className={FRAME}>
        <QuestionDecide approval={row.approval} said={row.said} onDecided={refresh} keys={false} />
      </div>
    );
  if (row.approval) return <ApprovalCard approval={row.approval} onDecided={refresh} />;
  if (row.proposal) return <ProposalCard proposal={row.proposal} onDecided={refresh} />;
  if (row.collection_proposal) return <CollectionProposalCard proposal={row.collection_proposal} onDecided={refresh} />;
  if (row.source === "push" && row.session)
    return (
      <div className={FRAME}>
        <h2 className="text-[15px] font-medium">{row.essence}</h2>
        <div>
          <PushButton session={row.session} count={row.push ?? 0} repo={row.repos?.length === 1 ? undefined : row.repos?.[0]} />
        </div>
      </div>
    );
  if (row.computer) return <ComputerWaits row={row} computer={row.computer} />;
  return (
    <div className={FRAME}>
      <p className="max-w-[68ch] break-words">{row.essence || (row.action === "reply" ? "It waits for your message." : "Its turn is done.")}</p>
      {row.session && <Reply session={row.session} placeholder={row.action === "reply" ? "It waits for your message" : "Reply to go on with it"} />}
    </div>
  );
}

/** A computer's session that waits on him with no card of its own: his words, answered here, reach it on that
 * computer's next report. */
function ComputerWaits({ row, computer }: { row: FeedRow; computer: NonNullable<FeedRow["computer"]> }) {
  const [said, setSaid] = useState<string | null>(row.answered ?? null);
  const deliver = deliverTo(row);
  return (
    <div className={FRAME}>
      <p className="max-w-[68ch] break-words">{row.essence || "It waits for your message."}</p>
      {said !== null ? (
        <div className="flex flex-col gap-1">
          <p className="rounded-md bg-muted px-3 py-2 text-sm">You: «{said}»</p>
          <p className="text-[13px] text-muted-foreground">It reaches the session when {computer.device} next checks in.</p>
        </div>
      ) : (
        deliver && <Reply session={`${computer.device}/${computer.id}`} deliver={deliver} placeholder="It waits for your message" onSent={setSaid} />
      )}
    </div>
  );
}

/** The waiting cards' numbers, each its own while it waits, kept on the device. */
function useNumbers(waiting: string[], now: number): Numbers {
  const key = waiting.join("\n");
  // biome-ignore lint/correctness/useExhaustiveDependencies: `key` is `waiting`'s content; `now` only stamps a freed one
  const numbers = useMemo(() => renumber(keptNumbers(), waiting, now), [key]);
  useEffect(() => keepNumbers(numbers), [numbers]);
  return numbers;
}

/** The operator's numbers of every waiting card, the same on «Today», a project's «Operator», the map and to the
 * header's microphone. */
export function useOperatorNumbers(feed: Feed | null, now: number): Numbers {
  return useNumbers(waitingKeys(feed), now);
}

/** One request as its card: the top line (its number while it waits, its project, its title, what it asks, since
 * when) over the card drawn by how its turn ended — the operator's, the map's pane alike. */
export function OperatorCard({ row, n, spheres, now, off = false }: { row: FeedRow; n?: number; spheres: Sphere[]; now: number; off?: boolean }) {
  return (
    <article
      aria-label={row.title}
      data-testid="operator-card"
      data-kind={endingOf(row)?.kind ?? row.action}
      className={cn("flex flex-col gap-2 transition-opacity", off && "opacity-50")}
    >
      <Crumb session={row.session}>
        {n !== undefined && <span className="mr-1.5 font-mono font-medium text-foreground tabular-nums">#{n}</span>}
        {row.project ? `${projectTitle(row.project, spheres)} · ` : ""}
        {/* the title is the way into the session's own screen, where the whole conversation goes on */}
        {row.session && !row.computer ? (
          <Link to="/s/$id" params={{ id: row.session }} className="text-foreground underline-offset-4 hover:underline">
            {row.title}
          </Link>
        ) : (
          row.title
        )}
        {row.computer ? ` · on ${row.computer.device}` : ""} · {verbOf(row)} · {off ? "answered or gone" : waited(row.since, now)}
      </Crumb>
      <Body row={row} />
    </article>
  );
}

export function Operator({
  feed,
  spheres,
  now,
  head,
  project,
  empty = "No session is asking you for anything.",
}: {
  feed: Feed | null;
  spheres: Sphere[];
  now: number;
  head?: React.ReactNode;
  /** Only this project's and its sub-projects' cards (its page's «Operator»). The numbers stay the ones «Today»
   * gives: «3» is the same card on either screen and to the header's microphone. */
  project?: string;
  empty?: string;
}) {
  const of = useProjectOf(spheres);
  const rows = requests(feed, project ? { path: project, of } : undefined);
  // the last word of every card shown, so one gone from the feed keeps its place with what it said
  const last = useRef(new Map<string, FeedRow>());
  for (const r of rows) last.current.set(r.key, r);
  const { shown, incoming, gone } = useHeld(rows.map((r) => r.key));
  const numbers = useOperatorNumbers(feed, now);
  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex w-full max-w-[720px] flex-col gap-6 px-4 pt-3 pb-16 md:px-6">
        {head}
        {incoming.length > 0 && (
          <div>
            <Button variant="outline" size="sm" onClick={releaseHeld}>
              New: {incoming.length} — show
            </Button>
          </div>
        )}
        {!feed ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : shown.length === 0 ? (
          <p className="text-sm text-muted-foreground">{empty}</p>
        ) : (
          shown.map((key) => {
            const row = last.current.get(key);
            if (!row) return null;
            const off = gone.has(key);
            const n = waits(row) && !off ? numbers[key]?.n : undefined;
            return <OperatorCard key={key} row={row} n={n} spheres={spheres} now={now} off={off} />;
          })
        )}
      </div>
    </div>
  );
}
