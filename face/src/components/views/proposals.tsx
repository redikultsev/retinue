// A Space's proposed writes: what the agents asked the owner to write to its collections (retinue/colltools.py),
// first on its screen, beside what waits for him — read on mount and again whenever the stream tells of a proposal of
// this Space, or resyncs. Nothing shows while none waits.
import { useCallback, useEffect, useState } from "react";
import { CollectionProposalCard } from "@/components/collection-proposal-card";
import { type ProposalsApi, proposalsApi } from "@/lib/edit";
import { type Live, live as theLive } from "@/lib/live";
import { LIST } from "@/lib/utils";
import type { CollectionProposal } from "@/lib/view-types";

export function SpaceProposals({ space, api = proposalsApi, source = theLive }: { space: string; api?: ProposalsApi; source?: Live }) {
  const [waiting, setWaiting] = useState<CollectionProposal[]>([]);
  const read = useCallback(async () => {
    try {
      setWaiting((await api.waiting(space)).proposals);
    } catch {
      // a platform without proposals, or none reachable: the screen shows the rest
    }
  }, [api, space]);
  useEffect(() => {
    void read();
    const off = source.onCollectionProposal((p) => {
      if (p.space === space) void read();
    });
    const again = source.onResync(() => void read());
    return () => {
      off();
      again();
    };
  }, [read, source, space]);
  if (waiting.length === 0) return null;
  return (
    <section className="flex flex-col gap-1.5" aria-label="Proposed">
      <div className="flex min-h-8 items-center gap-2 px-1">
        <h2 className="text-[13px] font-medium">Proposed</h2>
        <span className="font-mono text-xs text-muted-foreground tabular-nums">{waiting.length}</span>
      </div>
      <ul className={LIST}>
        {waiting.map((p) => (
          <CollectionProposalCard key={p.id} row proposal={p} api={api} onDecided={() => void read()} />
        ))}
      </ul>
    </section>
  );
}
