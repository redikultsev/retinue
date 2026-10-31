// `/s/<id>`: one session; on a wide screen with `?with=<id>` two side by side (E8), resizable, over the one stream.
// The sidebar and the right pane are the shell's (components/shell.tsx); a code session offers its diff there.
import { useNavigate } from "@tanstack/react-router";
import { Group, Panel, Separator } from "react-resizable-panels";
import { GitBranch } from "@/components/icons";
import { type Tab, SessionPanel } from "@/components/session-panel";
import { Button } from "@/components/ui/button";
import { useLiveState } from "@/lib/live";
import { useVeryWide, useWide } from "@/lib/media";
import { usePane } from "@/lib/pane";

export interface SessionSearch {
  with?: string;
  tab?: Tab;
  tab2?: Tab;
  /** The desktop's right pane (lib/pane.ts): kept while the tabs and panels change. */
  pane?: string;
}

export function SessionPage({ id, search }: { id: string; search: SessionSearch }) {
  const wide = useWide();
  const veryWide = useVeryWide();
  const navigate = useNavigate();
  const go = (next: { id: string; search: SessionSearch }) =>
    navigate({ to: "/s/$id", params: { id: next.id }, search: { pane: search.pane, ...next.search } });
  const tab = search.tab ?? "chat";
  const second = veryWide && search.with && search.with !== id ? search.with : undefined;

  if (!wide)
    return (
      <div className="h-full">
        <SessionPanel id={id} tab={tab} onTab={(t) => go({ id, search: { ...search, tab: t } })} back />
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
                onClose={() => go({ id: second, search: { tab: search.tab2 } })}
              />
            </Panel>
            <Separator className="w-1.5 bg-border transition-colors hover:bg-primary/40 data-[separator=active]:bg-primary/60" />
            <Panel id="right" minSize="25" defaultSize="50">
              <SessionPanel
                id={second}
                tab={search.tab2 ?? "chat"}
                onTab={(t) => go({ id, search: { ...search, tab2: t } })}
                onClose={() => go({ id, search: { tab: search.tab } })}
              />
            </Panel>
          </Group>
        ) : (
          <SessionPanel id={id} tab={tab} onTab={(t) => go({ id, search: { ...search, tab: t } })} extra={<DiffBeside id={id} />} panel />
        )}
      </main>
    </div>
  );
}

/** A code session's diff in the right pane, beside its transcript. */
function DiffBeside({ id }: { id: string }) {
  const { sessions } = useLiveState();
  const { pane, open } = usePane();
  if (sessions[id]?.kind !== "code" || (pane?.kind === "diff" && pane.session === id)) return null;
  return (
    <Button variant="ghost" size="icon" aria-label="Changes on the right" title="Changes on the right" onClick={() => open({ kind: "diff", session: id })}>
      <GitBranch />
    </Button>
  );
}
