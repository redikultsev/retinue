// `/s/<id>`: one session; on a wide screen with `?with=<id>` two side by side (E8), resizable, over the one stream.
// The sidebar and the right pane are the shell's (components/shell.tsx); a code session's diff opens there from its
// «Results».
import { useNavigate } from "@tanstack/react-router";
import { Group, Panel, Separator } from "react-resizable-panels";
import { type Tab, SessionPanel } from "@/components/session-panel";
import { useVeryWide, useWide } from "@/lib/media";

export interface SessionSearch {
  with?: string;
  tab?: Tab;
  tab2?: Tab;
  /** The desktop's right pane (lib/pane.ts): kept while the tabs and panels change. */
  pane?: string;
  /** A turn to show: the transcript opens at its summary (a Base commit links here). */
  turn?: number;
}

export function SessionPage({ id, search }: { id: string; search: SessionSearch }) {
  const wide = useWide();
  const veryWide = useVeryWide();
  const navigate = useNavigate();
  const go = (next: { id: string; search: SessionSearch }) =>
    navigate({ to: "/s/$id", params: { id: next.id }, search: { pane: search.pane, ...next.search } });
  // absent: the tab the session opens on (a finished one on «Results»)
  const tab = search.tab;
  const second = veryWide && search.with && search.with !== id ? search.with : undefined;

  if (!wide)
    return (
      <div className="h-full">
        <SessionPanel id={id} tab={tab} onTab={(t) => go({ id, search: { ...search, tab: t } })} back turn={search.turn} />
      </div>
    );

  return (
    <div className="flex h-full">
      <main className="min-w-0 flex-1">
        {second ? (
          <Group orientation="horizontal" className="h-full">
            <Panel id="left" minSize="25" defaultSize="50">
              <SessionPanel
                id={id}
                tab={tab}
                onTab={(t) => go({ id, search: { ...search, tab: t } })}
                turn={search.turn}
                onClose={() => go({ id: second, search: { tab: search.tab2 } })}
              />
            </Panel>
            <Separator className="w-1.5 bg-border transition-colors hover:bg-primary/40 data-[separator=active]:bg-primary/60" />
            <Panel id="right" minSize="25" defaultSize="50">
              <SessionPanel
                id={second}
                tab={search.tab2}
                onTab={(t) => go({ id, search: { ...search, tab2: t } })}
                onClose={() => go({ id, search: { tab: search.tab } })}
              />
            </Panel>
          </Group>
        ) : (
          <SessionPanel id={id} tab={tab} onTab={(t) => go({ id, search: { ...search, tab: t } })} panel turn={search.turn} />
        )}
      </main>
    </div>
  );
}
